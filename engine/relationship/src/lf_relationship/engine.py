"""관계 갱신의 순수 로직 (ADR-016 §갱신 규칙).

전부 결정적 순수 함수 — 같은 (상태, 사건, 파라미터) → 같은 결과.
LLM 호출·I/O 금지 (ADR-015와 동일 논거). 저장·이벤트 적재는 어댑터의 몫.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Any

import yaml

from lf_relationship.model import DIMENSIONS, RelationshipState, clamp_dimension

Direction = str  # "outgoing" | "incoming"


@cache
def default_params() -> dict[str, Any]:
    """params.yaml — 델타 튜닝의 단일 원천 (ADR-016 완화책)."""
    text = (files("lf_relationship") / "params.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


@dataclass(frozen=True)
class UpdateResult:
    state: RelationshipState
    #: 발행 임계를 넘었나 — True면 호출자가 relationship.state.changed를 적재하고
    #: consume_pending()으로 누적분을 비운다
    publish: bool
    reason: str


def _apply_deltas(
    state: RelationshipState, deltas: dict[str, float], *, salience_delta: float,
    reason: str, params: dict[str, Any],
) -> UpdateResult:
    dimensions = dict(state.dimensions)
    pending = dict(state.pending)
    for name, delta in deltas.items():
        before = dimensions[name]
        dimensions[name] = clamp_dimension(name, before + delta)
        pending[name] += dimensions[name] - before  # 클램프 반영된 실효 델타만 누적
    new_state = RelationshipState(
        dimensions=dimensions,
        stage=state.stage,
        salience=min(1.0, max(0.0, state.salience + salience_delta)),
        pending=pending,
    )
    publish = new_state.pending_l1() >= params["publish_threshold"]
    return UpdateResult(state=new_state, publish=publish, reason=reason)


def apply_interaction(
    state: RelationshipState,
    source_kind: str,
    direction: Direction,
    *,
    params: dict[str, Any] | None = None,
) -> UpdateResult:
    """상호작용 효과 (갱신 규칙 1). source_kind는 params의 interaction_effects 키다."""
    params = params or default_params()
    effect = params["interaction_effects"].get(source_kind, {}).get(direction)
    if not effect:
        return UpdateResult(state=state, publish=False, reason="")
    deltas = {k: v for k, v in effect.items() if k in DIMENSIONS}
    return _apply_deltas(
        state, deltas,
        salience_delta=effect.get("salience", 0.0),
        reason=f"{source_kind} ({direction})",
        params=params,
    )


def consolidate_emotion(
    state: RelationshipState,
    emotion_type: str,
    intensity: float,
    *,
    params: dict[str, Any] | None = None,
) -> UpdateResult:
    """감정 응고 (갱신 규칙 2) — 대상 있는 감정이 관계 차원으로 스며든다.

    반복 anger → resentment 누적이 여기서 일어난다 (독립 축 — 화해 후에도 앙금).
    """
    params = params or default_params()
    coefficients = params["emotion_consolidation"].get(emotion_type)
    if not coefficients:
        return UpdateResult(state=state, publish=False, reason="")
    deltas = {k: v * intensity for k, v in coefficients.items() if k in DIMENSIONS}
    return _apply_deltas(
        state, deltas,
        salience_delta=0.02 * intensity,  # 감정이 일었던 상대는 삶에서 비중이 는다
        reason=f"감정 응고: {emotion_type} {intensity:.2f}",
        params=params,
    )


def decay(
    state: RelationshipState, ticks: int, *, params: dict[str, Any] | None = None
) -> RelationshipState:
    """시간 감쇠 (갱신 규칙 4) — 상호작용 없으면 intimacy·salience가 식는다.

    resentment는 매우 느리게 — 잊히지만 사라지지 않는다. 감쇠분도 pending에
    누적되어, 오래 식은 관계는 결국 state.changed로 세계에 기록된다.
    """
    if ticks <= 0:
        return state
    params = params or default_params()
    rates = params["decay_per_tick"]
    dimensions = dict(state.dimensions)
    pending = dict(state.pending)
    for name, rate in rates.items():
        if name not in DIMENSIONS:
            continue
        before = dimensions[name]
        dimensions[name] = clamp_dimension(name, before - rate * ticks)
        pending[name] += dimensions[name] - before
    salience = max(0.0, state.salience - rates.get("salience", 0.0) * ticks)
    return RelationshipState(
        dimensions=dimensions, stage=state.stage, salience=salience, pending=pending
    )


def consume_pending(state: RelationshipState) -> tuple[RelationshipState, dict[str, float]]:
    """발행 시점 — 누적 델타를 돌려주고 pending을 비운다."""
    deltas = {k: round(v, 4) for k, v in state.pending.items()}
    cleared = RelationshipState(
        dimensions=state.dimensions,
        stage=state.stage,
        salience=state.salience,
        pending={k: 0.0 for k in DIMENSIONS},
    )
    return cleared, deltas


def transition_stage(state: RelationshipState, stage: str) -> RelationshipState:
    """stage 전이 — 수치가 아니라 이벤트(행동·마일스톤)가 만든다 (ADR-016)."""
    return RelationshipState(
        dimensions=state.dimensions, stage=stage,
        salience=state.salience, pending=state.pending,
    )
