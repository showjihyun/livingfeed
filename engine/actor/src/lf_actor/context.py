"""Context Fabric 최소 구현 (ADR-009).

섹션 순서는 고정이다 — 변동성 낮은 것부터. 이 순서가 prompt cache 적중률을
만든다 (ADR-018 §2). 순서 변경은 ADR-009 대체 없이는 금지.

Phase 1 범위: Identity(1) / Working(5) / World(6) / Task Frame(7).
Beliefs(2)·Relationship(3)·Episodes(4)는 해당 계층(Qdrant/Kuzu) 도입 시 채워진다.
순수 함수적: 같은 입력 → 같은 번들 (trace_id 제외). LLM 호출·상태 변경 없음.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lf_eventstore import new_ulid

from lf_actor.persona import Persona

#: 문자 기반 토큰 근사 — 예산 집행용 보수적 환산 (한글 혼용 기준 1 token ≈ 2.5 chars)
CHARS_PER_TOKEN = 2.5

#: 섹션별 토큰 예산 (ADR-009 §토큰 예산, Phase 1 기본값)
BUDGET_IDENTITY = 800
BUDGET_EPISODES = 600
BUDGET_WORKING = 1_200
BUDGET_WORLD = 400
BUDGET_TASK_FRAME = 600


@dataclass(frozen=True)
class WorldContext:
    world_id: str
    tick: int
    world_time: datetime


@dataclass(frozen=True)
class Bundle:
    """AI Runtime으로 넘어가는 조립 결과 (ADR-009 ContextBundle).

    system = 정적 프리픽스(Identity), user = 변동 섹션들.
    """

    system: str
    user: str
    trace_id: str


def _clip(text: str, token_budget: int) -> str:
    limit = int(token_budget * CHARS_PER_TOKEN)
    return text if len(text) <= limit else text[:limit]


def _identity_section(persona: Persona) -> str:
    goals = "; ".join(
        f"{g.get('description')} (우선순위 {g.get('priority')})" for g in persona.goals
    )
    needs = ", ".join(f"{k}={v}" for k, v in sorted(persona.needs_bias.items()))
    traits = ", ".join(f"{k}={v}" for k, v in sorted(persona.big_five.items()))
    text = (
        f"당신은 '{persona.name}'({persona.id}) — {persona.archetype}.\n"
        f"{persona.identity_core}\n"
        f"성격(Big Five): {traits}\n"
        f"욕구 편향: {needs}\n"
        f"목표: {goals}\n"
        "당신은 살아있는 세계의 주민이다. 항상 이 인물로서 지각하고 결정한다."
    )
    return _clip(text, BUDGET_IDENTITY)


def _episodes_section(episodes: list[str]) -> str:
    """Episodes(4) — 장기 기억 회상 (ADR-008 recall). 비어 있으면 섹션 생략이 아니라
    고정 문구를 둔다 — 섹션 존재 자체가 프리픽스 안정성에 기여한다 (ADR-009/018)."""
    if not episodes:
        return "## 떠오르는 기억\n(지금 상황과 이어지는 오래된 기억은 없다)"
    limit = int(BUDGET_EPISODES * CHARS_PER_TOKEN)
    kept: list[str] = []
    used = 0
    for episode in episodes:
        if used + len(episode) > limit:
            break
        kept.append(episode)
        used += len(episode) + 1
    return "## 떠오르는 기억 (관련 회상)\n" + "\n".join(f"- {e}" for e in kept)


def _conversation_section(turns: list[tuple[str, str]], *, player_label: str = "관찰자") -> str:
    """이 사람과의 대화를 시간순으로 (reply_to_player 전용, ADR-009).

    working이 최신순으로 뒤섞여 들어가면 LLM이 대화를 흐름으로 못 읽는다 —
    여기서 오래된→최근 순으로 펴 주고, 마지막 상대 발화에 답할 지점을 찍는다.
    """
    if not turns:
        return ""
    last_player = max(
        (i for i, (who, _) in enumerate(turns) if who == "player"), default=-1
    )
    lines: list[str] = []
    for i, (who, text) in enumerate(turns):
        label = "나" if who == "me" else player_label
        marker = "   ← 지금 이 말에 답하라" if i == last_player else ""
        lines.append(f"- {label}: {text}{marker}")
    return "## 이 사람과 나눈 대화 (오래된 순)\n" + "\n".join(lines)


def _working_section(entries: list[str]) -> str:
    """최근 우선 절단 (ADR-009 규칙 2) — 예산 안에서 최신 항목부터 담는다."""
    limit = int(BUDGET_WORKING * CHARS_PER_TOKEN)
    kept: list[str] = []
    used = 0
    for entry in entries:  # entries는 최신 우선
        if used + len(entry) > limit:
            break
        kept.append(entry)
        used += len(entry) + 1
    if not kept:
        return "## 작업 기억\n(최근 기억 없음 — 방금 깨어났다)"
    return "## 작업 기억 (최신 우선)\n" + "\n".join(f"- {e}" for e in kept)


def _world_section(world: WorldContext) -> str:
    text = (
        "## 세계 상황\n"
        f"- 세계: {world.world_id}, tick {world.tick}\n"
        f"- 세계 시간: {world.world_time.isoformat()}"
    )
    return _clip(text, BUDGET_WORLD)


_TASK_FRAMES = {
    "decide_action": (
        "## 임무\n"
        "지금 이 tick에서 당신이 할 행동 하나를 결정하라.\n"
        "- 행동은 성격·욕구·목표·작업 기억과 일관되어야 한다.\n"
        "- intent는 피드 내레이션에 쓰일 한 줄 요약이다 — 구체적이고 이 인물답게.\n"
        "- 출력은 지정된 JSON 스키마를 정확히 따른다. JSON 외 텍스트 금지."
    ),
    "reply_to_player": (
        "## 임무\n"
        "위 '이 사람과 나눈 대화'가 지금까지의 흐름이다. 표시된 마지막 상대 발화에 답하라.\n"
        "- 흐름을 이어서 답한다: 상대가 물으면 실제로 답하고(예/아니오·구체적인 말),\n"
        "  같은 감사 인사('고마워')를 매번 반복하지 않는다.\n"
        "- 말투를 유지한다 — 당신이 앞서 반말이었으면 반말, 존댓말이었으면 존댓말.\n"
        "- 이 인물답게 1~3문장. 과장 금지 — 관계의 온도와 지금 상황에 맞게.\n"
        "- 상대는 세계 밖의 관찰자지만, 당신에게는 그냥 아는 사람이다.\n"
        '- 출력은 {"text": "..."} JSON 하나뿐이다. JSON 외 텍스트 금지.'
    ),
}


def build(
    persona: Persona,
    working: list[str],
    world: WorldContext,
    *,
    purpose: str = "decide_action",
    trace_id: str | None = None,
    episodes: list[str] | None = None,
    conversation: list[tuple[str, str]] | None = None,
) -> Bundle:
    """ContextBundle 조립 — 섹션 순서 고정 (ADR-009 규칙 1: Episodes(4) < Working(5)).

    conversation(reply_to_player 전용)이 있으면 Working 앞에 대화 흐름 섹션을
    끼운다 — 답장은 뒤섞인 버퍼가 아니라 시간순 대화를 보고 써야 한다.
    """
    frame = _TASK_FRAMES.get(purpose)
    if frame is None:
        raise ValueError(f"알 수 없는 purpose: {purpose}")
    sections = [_episodes_section(episodes or [])]
    if conversation:
        sections.append(_conversation_section(conversation))
    sections += [
        _working_section(working),
        _world_section(world),
        _clip(frame, BUDGET_TASK_FRAME),
    ]
    return Bundle(
        system=_identity_section(persona),
        user="\n\n".join(sections),
        trace_id=trace_id or new_ulid(),
    )
