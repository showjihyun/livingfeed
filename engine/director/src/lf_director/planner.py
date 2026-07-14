"""LLM 개입 선택 — 관찰은 규칙, '어떤 개입이 서사적으로 유효한가'만 LLM (ADR-013).

순수 로직만 여기 있다(프롬프트 조립 + 응답→Intervention 매핑). 모델 호출은
client.DirectorAiClient가, 발화 게이트는 rules.is_fireable가 담당한다.

hard rule은 매핑 단계에서 방어적으로 재집행된다 — LLM은 행동 반경을 넓힐 수 없다:
- incident_kind는 닫힌 라이브러리(params.yaml) 안에서만 (enum + 매핑 재확인)
- 영향권은 관찰된 후보 액터 안에서만 (환각 id는 버린다)
- location_id는 라이브러리 항목에서만 (LLM이 장소를 지어내지 못한다)
- intensity는 [0,1]로 클램프
LLM이 기여하는 것은 '무대 배치'(어떤 종류를, 누구를, 어떤 서술로)뿐이다.
"""

from __future__ import annotations

from typing import Any

from lf_director.rules import Intervention
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
    """director_plan 응답 스키마 — incident_kind는 라이브러리 enum(도구 인자 화이트리스트)."""
    return {
        "type": "object",
        "properties": {
            "incident_kind": {"type": "string", "enum": incident_kinds},
            "affected_actor_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 4,
            },
            "description": {"type": "string", "minLength": 1, "maxLength": 300},
            "intensity": {"type": "number", "minimum": 0, "maximum": 1},
            "rationale": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "required": [
            "incident_kind", "affected_actor_ids", "description", "intensity", "rationale"
        ],
        "additionalProperties": False,
    }


DIRECTOR_SYSTEM = (
    "너는 살아있는 세계의 '연출가(Director)'다. 규칙: 너는 액터를 조종하지 않는다. "
    "환경 사건 하나를 무대에 놓아 침체된 서사에 밀도를 되돌린다 — 반응은 액터의 몫이다. "
    "반드시 제공된 '사건 종류' 중에서만 고르고, '영향권'은 제시된 후보 액터 중에서만 고른다. "
    "없는 인물·장소를 지어내지 마라. 서술은 특정 인물의 긴장에 뿌리내린 한 줄로, "
    "누구를 조종하는 명령이 아니라 그저 벌어진 환경의 묘사여야 한다."
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
    lines += ["", "## 놓을 수 있는 사건 종류 (이 목록이 전부다)"]
    for inc in incidents:
        loc = inc.get("location_id") or "장소 무관"
        lines.append(f"- {inc['kind']}: {inc['description']} [{loc}, 기본 강도 {inc['intensity']}]")
    candidates = candidate_actor_ids(tension_pairs)
    cand_text = ", ".join(f"{_name(a, names)}({a})" for a in candidates) or "(없음)"
    lines += [
        "",
        "## 할 일",
        "가장 서사적으로 유효한 사건 하나를 골라라. incident_kind는 위 종류 중 하나, "
        f"affected_actor_ids는 다음 후보 중에서만: {cand_text}. "
        "description은 그 인물들의 긴장에 맞춘 한 줄로 새로 써라(템플릿을 그대로 쓰지 마라). "
        "intensity는 0~1, rationale은 왜 이 개입이 지금 유효한지 한 줄.",
    ]
    return "\n".join(lines)


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, v))


def intervention_from_plan(
    plan: dict[str, Any],
    snapshot: Snapshot,
    tension_pairs: list[list[Any]],
    incidents: list[dict[str, Any]],
    names: dict[str, str],
    *,
    model: str | None = None,
) -> Intervention | None:
    """검증된 LLM 응답 → Intervention. hard rule을 방어적으로 재집행한다.

    무효(화이트리스트 밖 kind 등)면 None — 호출자가 규칙 폴백한다.
    """
    library = {inc["kind"]: inc for inc in incidents}
    inc = library.get(plan.get("incident_kind"))
    if inc is None:
        return None  # 라이브러리 밖 사건 — 존재하지 않는 도구다

    # 영향권은 관찰된 후보 안에서만 (환각 id 제거). 비었고 긴장이 있으면 상위 쌍으로.
    candidates = set(candidate_actor_ids(tension_pairs))
    affected = [a for a in plan.get("affected_actor_ids", []) if a in candidates]
    if not affected and tension_pairs:
        affected = [tension_pairs[0][0], tension_pairs[0][1]]

    description = (plan.get("description") or "").strip()[:300] or inc["description"]
    intensity = _clamp(plan.get("intensity"), 0.0, 1.0, float(inc["intensity"]))
    rationale = (plan.get("rationale") or "").strip()[:300] or "LLM 개입 선택"

    return Intervention(
        tool="inject_incident",
        incident_kind=inc["kind"],
        description=description,
        location_id=inc.get("location_id"),  # 장소는 라이브러리에서만 (LLM이 못 지어낸다)
        affected_actor_ids=affected,
        intensity=intensity,
        reason=rationale,
        signals={
            "drama_ma": snapshot.drama_ma,
            "quiet_ticks": snapshot.quiet_ticks,
            "tension_top": tension_pairs[:3],
            "selector": "llm",  # 개입 선택 주체 — 감사에서 규칙/LLM 구분 (ADR-013)
            "model": model,
        },
    )
