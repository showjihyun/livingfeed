"""LLM 개입 선택 — 관찰은 규칙, '어떤 개입이 서사적으로 유효한가'만 LLM (ADR-013).

순수 로직만 여기 있다(프롬프트 조립 + 응답→Intervention 매핑). 모델 호출은
client.DirectorAiClient가, 발화 게이트는 rules.is_fireable가 담당한다.

LLM은 도구 화이트리스트 중 하나를 고른다:
- inject_incident: 공개 환경 사건 (world.incident.occurred → 세계 뉴스)
- nudge_perception: 한 사람에게 사적 관측을 심는다 (world.observation.surfaced — 비공개)

hard rule은 매핑 단계에서 방어적으로 재집행된다 — LLM은 행동 반경을 넓힐 수 없다:
- 도구는 화이트리스트 안에서만 (밖이면 None → 규칙 폴백)
- incident_kind는 닫힌 라이브러리(params.yaml) 안에서만, location도 라이브러리에서만
- 대상(영향권·nudge 타깃·about)은 관찰된 후보 액터 안에서만 (환각 id는 버린다)
- intensity는 [0,1]로 클램프
LLM이 기여하는 것은 '무대 배치'(어떤 도구를, 누구에게, 어떤 서술로)뿐이다.
"""

from __future__ import annotations

from typing import Any

from lf_director.rules import (
    INCIDENT_STREAM_KEY,
    INCIDENT_TOOL,
    INCIDENT_TYPE,
    NUDGE_TOOL,
    OBSERVATION_STREAM_KEY,
    OBSERVATION_TYPE,
    Intervention,
)
from lf_director.signals import Snapshot


def candidate_actor_ids(tension_pairs: list[list[Any]]) -> list[str]:
    """긴장 질의에 등장한 액터 — LLM 영향권 선택의 후보 집합(이 밖은 환각)."""
    seen: list[str] = []
    for pair in tension_pairs:
        for actor_id in pair[:2]:
            if isinstance(actor_id, str) and actor_id not in seen:
                seen.append(actor_id)
    return seen


def plan_schema(incident_kinds: list[str]) -> dict[str, Any]:
    """director_plan 응답 스키마 — tool로 도구를 고르고 도구별 인자를 채운다.

    도구별 인자는 선택적(optional)이고, 필수는 tool·rationale뿐이다 — 어떤 인자가
    유효/필수인지는 intervention_from_plan이 도구별로 재집행한다(하드룰). incident_kind
    enum만 스키마에서 직접 좁힌다(닫힌 라이브러리 = 도구 인자 화이트리스트).
    """
    return {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": [INCIDENT_TOOL, NUDGE_TOOL]},
            # inject_incident 인자
            "incident_kind": {"type": "string", "enum": incident_kinds},
            "affected_actor_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            "description": {"type": "string", "maxLength": 300},
            "intensity": {"type": "number", "minimum": 0, "maximum": 1},
            # nudge_perception 인자
            "target_actor_id": {"type": "string"},
            "observation": {"type": "string", "maxLength": 300},
            "about_actor_id": {"type": ["string", "null"]},
            # 공통
            "rationale": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "required": ["tool", "rationale"],
        "additionalProperties": False,
    }


DIRECTOR_SYSTEM = (
    "너는 살아있는 세계의 '연출가(Director)'다. 규칙: 너는 액터를 조종하지 않는다. "
    "침체된 서사에 밀도를 되돌리되 직접 결정을 쓰지 않고 간접적으로만 개입한다 — "
    "반응은 액터의 몫이다. "
    "두 도구 중 하나를 고른다: 공개 환경 사건을 놓거나(inject_incident), "
    "후보 액터 한 명에게 사적 관측을 심는다(nudge_perception — 그 사람만 알아차린다). "
    "제시된 후보 액터·사건 종류 안에서만 고르고, 없는 인물·장소를 지어내지 마라. "
    "서술은 특정 인물의 긴장에 뿌리내린 한 줄이어야 하며, 조종하는 명령이 아니다."
)


def _name(actor_id: str, names: dict[str, str]) -> str:
    return names.get(actor_id, actor_id)


def build_plan_user(
    snapshot: Snapshot,
    tension_pairs: list[list[Any]],
    incidents: list[dict[str, Any]],
    names: dict[str, str],
) -> str:
    """개입 선택 프롬프트의 user 섹션 — 신호·긴장 후보·사건 라이브러리를 근거로 제시."""
    lines = [
        "## 상황",
        f"세계가 침체됐다 — drama 이동평균 {snapshot.drama_ma}, "
        f"{snapshot.quiet_ticks} tick째 조용하다.",
        "",
        "## 긴장 후보 (그래프 질의 — 원한 상위 방향 엣지)",
    ]
    if tension_pairs:
        for pair in tension_pairs[:5]:
            frm, to = _name(pair[0], names), _name(pair[1], names)
            resentment = pair[2] if len(pair) > 2 else "?"
            lines.append(f"- {frm} → {to} (원한 {resentment})")
    else:
        lines.append("- (뚜렷한 긴장 없음 — 영향권 없이 순수 환경 사건도 가능)")
    lines += ["", "## inject_incident로 놓을 수 있는 사건 종류 (이 목록이 전부다)"]
    for inc in incidents:
        loc = inc.get("location_id") or "장소 무관"
        lines.append(f"- {inc['kind']}: {inc['description']} [{loc}, 기본 강도 {inc['intensity']}]")
    candidates = candidate_actor_ids(tension_pairs)
    cand_text = ", ".join(f"{_name(a, names)}({a})" for a in candidates) or "(없음)"
    lines += [
        "",
        "## 할 일 — 도구 하나를 골라 tool에 적고 그 인자를 채워라",
        f"후보 액터(대상은 여기서만 고른다): {cand_text}",
        "",
        "· tool=inject_incident (공개 사건 → 세계 뉴스): incident_kind는 위 종류 중 하나, "
        "affected_actor_ids는 후보 중에서, description은 그 인물들의 긴장에 맞춘 한 줄"
        "(템플릿 복사 금지), intensity는 0~1.",
        "· tool=nudge_perception (사적 관측 — 소동 없이 한 사람의 다음 선택을 흔든다): "
        "target_actor_id는 후보 한 명, observation은 그가 문득 알아차리는 것 한 줄, "
        "about_actor_id는 그 관측이 향한 상대(후보 중 하나, 없으면 null).",
        "",
        "rationale은 왜 지금 이 개입인지 한 줄.",
    ]
    return "\n".join(lines)


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, v))


def _signals(
    snapshot: Snapshot, tension_pairs: list[list[Any]], model: str | None
) -> dict[str, Any]:
    return {
        "drama_ma": snapshot.drama_ma,
        "quiet_ticks": snapshot.quiet_ticks,
        "tension_top": tension_pairs[:3],
        "selector": "llm",  # 개입 선택 주체 — 감사에서 규칙/LLM 구분 (ADR-013)
        "model": model,
    }


def intervention_from_plan(
    plan: dict[str, Any],
    snapshot: Snapshot,
    tension_pairs: list[list[Any]],
    incidents: list[dict[str, Any]],
    names: dict[str, str],
    *,
    model: str | None = None,
) -> Intervention | None:
    """검증된 LLM 응답 → Intervention. 도구별로 hard rule을 방어적으로 재집행한다.

    무효(화이트리스트 밖 도구·kind, 없는 대상 등)면 None — 호출자가 규칙 폴백한다.
    """
    signals = _signals(snapshot, tension_pairs, model)
    rationale = (plan.get("rationale") or "").strip()[:300] or "LLM 개입 선택"
    tool = plan.get("tool")
    if tool == INCIDENT_TOOL:
        return _incident_intervention(plan, tension_pairs, incidents, rationale, signals)
    if tool == NUDGE_TOOL:
        return _nudge_intervention(plan, tension_pairs, rationale, signals)
    return None  # 화이트리스트 밖 도구 — 존재하지 않는다


def _incident_intervention(
    plan: dict[str, Any], tension_pairs: list[list[Any]],
    incidents: list[dict[str, Any]], rationale: str, signals: dict[str, Any],
) -> Intervention | None:
    library = {inc["kind"]: inc for inc in incidents}
    inc = library.get(plan.get("incident_kind"))
    if inc is None:
        return None  # 라이브러리 밖 사건 — 존재하지 않는다

    # 영향권은 관찰된 후보 안에서만 (환각 id 제거). 비었고 긴장이 있으면 상위 쌍으로.
    candidates = set(candidate_actor_ids(tension_pairs))
    affected = [a for a in plan.get("affected_actor_ids", []) if a in candidates]
    if not affected and tension_pairs:
        affected = [tension_pairs[0][0], tension_pairs[0][1]]

    description = (plan.get("description") or "").strip()[:300] or inc["description"]
    intensity = _clamp(plan.get("intensity"), 0.0, 1.0, float(inc["intensity"]))
    return Intervention(
        tool=INCIDENT_TOOL,
        event_type=INCIDENT_TYPE,
        stream_key=INCIDENT_STREAM_KEY,
        payload={
            "incident_kind": inc["kind"],
            "description": description,
            "location_id": inc.get("location_id"),  # 장소는 라이브러리에서만
            "affected_actor_ids": affected,
            "intensity": intensity,
        },
        reason=rationale,
        signals=signals,
    )


def _nudge_intervention(
    plan: dict[str, Any], tension_pairs: list[list[Any]],
    rationale: str, signals: dict[str, Any],
) -> Intervention | None:
    # 사적 관측은 후보 액터 한 명에게만 배달된다 — 없는 대상이면 배달 불가(규칙 폴백)
    candidates = set(candidate_actor_ids(tension_pairs))
    target = plan.get("target_actor_id")
    if target not in candidates:
        return None
    observation = (plan.get("observation") or "").strip()[:300]
    if not observation:
        return None  # 관측 없는 nudge는 의미 없다
    about = plan.get("about_actor_id")
    about = about if about in candidates else None  # 없는 상대는 버린다(관측 자체는 남는다)
    return Intervention(
        tool=NUDGE_TOOL,
        event_type=OBSERVATION_TYPE,
        stream_key=OBSERVATION_STREAM_KEY,
        payload={
            "target_actor_id": target,
            "observation": observation,
            "about_actor_id": about,
        },
        reason=rationale,
        signals=signals,
    )
