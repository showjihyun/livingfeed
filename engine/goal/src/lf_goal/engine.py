"""욕구·목표 갱신의 순수 로직 (ADR-012 §인지 루프 need/goal 갱신).

전부 결정적 순수 함수다 — 같은 (상태, 사건, 페르소나, 파라미터) → 같은 결과.
리플레이 완전 재현이 존재 조건이다. LLM 호출·I/O 금지.

핵심: 행동은 그것이 섬기는 '욕구'를 채우고, 그 욕구에 걸린 '목표'를 진행시킨다.
congruence(정렬도) = 액터가 그 욕구를 얼마나 중히 여기는가(needs_bias) — 자기 드라이브에
맞는 행동일수록 기억에 남고(ADR-008 중요도 0.20 항) 만족스럽다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from typing import Any

import yaml

from lf_goal.model import NEEDS, GoalState, clamp01

#: 행동 종류 → 섬기는 욕구 (docs/plan/06 욕구 모델의 투영, rules.fallback_action과 정합)
ACTION_NEED: dict[str, str] = {
    "work": "achievement",
    "confront": "achievement",
    "create": "achievement",
    "study": "achievement",
    "speak": "belonging",
    "help": "belonging",
    "observe": "belonging",
    "share": "belonging",
    "rest": "security",
    "move": "security",
    "avoid": "security",
}

#: 플레이어 상호작용 → 채워지는 욕구 (지지·관심은 소속 욕구를 채운다)
INTERACTION_NEED: dict[str, str] = {
    "player.dm.sent": "belonging",
    "player.comment.posted": "belonging",
    "player.reaction.added": "belonging",
}


@cache
def default_params() -> dict[str, Any]:
    text = (files("lf_goal") / "params.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


@dataclass(frozen=True)
class GoalAdvance:
    """발행 임계를 넘긴 목표 진행 — actor.goal.advanced의 재료."""

    goal_id: str
    description: str
    progress: float
    need: str
    congruence: float


@dataclass(frozen=True)
class AppraisalResult:
    state: GoalState
    #: 이 행동이 액터의 드라이브에 얼마나 맞았나 0..1 (기억 중요도 goal 항)
    congruence: float
    #: 임계를 넘어 세계에 기록할 목표 진행들
    advances: list[GoalAdvance] = field(default_factory=list)


def _care(needs_bias: dict[str, float], need: str, params: dict[str, Any]) -> float:
    return float(needs_bias.get(need, params["default_bias"]))


def initial_state(
    persona_goals: list[dict[str, Any]],
    needs_bias: dict[str, float],
    *,
    params: dict[str, Any] | None = None,
) -> GoalState:
    """페르소나에서 초기 상태 — 욕구는 반쯤 채워진 채, 목표는 진행 0."""
    params = params or default_params()
    fill = params["needs"]["initial_fill"]
    needs = {n: round(_care(needs_bias, n, params) * fill, 4) for n in NEEDS}
    goals = {str(g["id"]): 0.0 for g in persona_goals if g.get("id")}
    return GoalState(needs=needs, goals=goals, pending={})


def appraise_action(
    state: GoalState,
    action_kind: str,
    persona_goals: list[dict[str, Any]],
    needs_bias: dict[str, float],
    *,
    params: dict[str, Any] | None = None,
) -> AppraisalResult:
    """행동 하나를 평가한다 — 섬기는 욕구를 채우고 걸린 목표를 진행시킨다."""
    params = params or default_params()
    need = ACTION_NEED.get(action_kind)
    if need is None:
        return AppraisalResult(state=state, congruence=0.0)

    care = _care(needs_bias, need, params)
    needs = dict(state.needs)
    needs[need] = clamp01(needs[need] + params["needs"]["satisfy_per_action"])

    goals = dict(state.goals)
    pending = dict(state.pending)
    threshold = params["goals"]["publish_threshold"]
    step = params["goals"]["progress_step"]
    advances: list[GoalAdvance] = []
    for goal in persona_goals:
        if goal.get("need") != need:
            continue
        gid = str(goal["id"])
        priority = float(goal.get("priority", 0.5))
        before = goals.get(gid, 0.0)
        after = clamp01(before + step * priority)
        goals[gid] = after
        pend = pending.get(gid, 0.0) + (after - before)
        if pend >= threshold or after >= 1.0:
            advances.append(
                GoalAdvance(
                    goal_id=gid,
                    description=str(goal.get("description", gid))[:200],
                    progress=round(after, 4),
                    need=need,
                    congruence=round(care, 4),
                )
            )
            pend = 0.0
        pending[gid] = round(pend, 4)

    return AppraisalResult(
        state=GoalState(needs=needs, goals=goals, pending=pending),
        congruence=round(care, 4),
        advances=advances,
    )


def satisfy_from_interaction(
    state: GoalState,
    interaction_type: str,
    *,
    params: dict[str, Any] | None = None,
) -> GoalState:
    """플레이어 상호작용이 욕구를 채운다 (목표 진행 없음 — 관심은 위안이지 성취가 아니다)."""
    params = params or default_params()
    need = INTERACTION_NEED.get(interaction_type)
    if need is None:
        return state
    needs = dict(state.needs)
    needs[need] = clamp01(needs[need] + params["needs"]["satisfy_per_interaction"])
    return GoalState(needs=needs, goals=state.goals, pending=state.pending)


def decay(
    state: GoalState, ticks: int, *, params: dict[str, Any] | None = None
) -> GoalState:
    """tick 경과 — 욕구 만족도가 감쇠한다(욕구가 되돌아온다). 목표 진행은 남는다."""
    if ticks <= 0:
        return state
    params = params or default_params()
    drop = params["needs"]["deplete_per_tick"] * ticks
    needs = {n: clamp01(v - drop) for n, v in state.needs.items()}
    return GoalState(needs=needs, goals=state.goals, pending=state.pending)


def pressing_need(state: GoalState, needs_bias: dict[str, float]) -> str:
    """지금 가장 목마른 욕구 — 결핍(1-만족)·관심(bias) 곱이 큰 것 (decide 입력)."""
    params = default_params()
    return max(
        NEEDS,
        key=lambda n: (1.0 - state.needs.get(n, 0.0)) * _care(needs_bias, n, params),
    )


_NEED_KO = {"achievement": "인정·성취", "belonging": "소속·연결", "security": "안정"}


def describe(
    state: GoalState, persona_goals: list[dict[str, Any]], needs_bias: dict[str, float]
) -> str:
    """욕구·목표 요약 한 줄 (Context Fabric Working 섹션 주입 — 액터가 목표를 좇게)."""
    need = pressing_need(state, needs_bias)
    parts = [f"지금 가장 목마른 것: {_NEED_KO.get(need, need)}"]
    ranked = sorted(
        persona_goals, key=lambda g: state.goals.get(str(g.get("id")), 0.0), reverse=True
    )
    for goal in ranked[:2]:
        gid = str(goal.get("id"))
        progress = state.goals.get(gid, 0.0)
        parts.append(f"{goal.get('description', gid)} ({round(progress * 100)}%)")
    return " · ".join(parts)
