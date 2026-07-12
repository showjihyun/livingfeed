"""평가·감쇠의 순수 로직 (ADR-015 §appraisal/§감쇠).

전부 결정적 순수 함수다 — 같은 (상태, 사건, 성격, 파라미터) → 같은 결과.
리플레이 완전 재현이 이 모듈의 존재 조건이다. LLM 호출·I/O 금지.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Any

import yaml

from lf_emotion.model import (
    EmotionInstance,
    EmotionState,
    Pad,
    baseline_from_ocean,
    merge_instance,
)


@cache
def default_params() -> dict[str, Any]:
    """params.yaml — 파라미터 단일 원천 (ADR-015 완화책)."""
    text = (files("lf_emotion") / "params.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


@dataclass(frozen=True)
class AppraisalResult:
    state: EmotionState
    #: 발행 임계를 넘는 변화였나 (actor.emotion.shifted 대상)
    significant: bool
    #: 사람이 읽는 원인 한 줄 (이벤트 payload의 reason)
    reason: str


def _sensitivity(big_five: dict[str, float], params: dict[str, Any]) -> float:
    p = params["personality"]
    neuroticism = big_five.get("neuroticism", 0.5)
    extraversion = big_five.get("extraversion", 0.5)
    return (1 + neuroticism * p["sensitivity_neuroticism"]) * (
        1 + extraversion * p["social_extraversion"]
    )


def _resilience(big_five: dict[str, float], params: dict[str, Any]) -> float:
    neuroticism = big_five.get("neuroticism", 0.5)
    return 1 + (1 - neuroticism) * params["personality"]["resilience"]


def appraise_interaction(
    state: EmotionState,
    interaction: dict[str, Any],
    big_five: dict[str, float],
    *,
    params: dict[str, Any] | None = None,
) -> AppraisalResult:
    """플레이어 상호작용 봉투 하나를 평가한다 (OCC 최소 절단면 — 유형 기반).

    목표 일치도·행위자 책임 평가는 Goal/Relationship 상태가 읽을 수 있게 되는
    단계(ADR-016)에서 확장된다.
    """
    params = params or default_params()
    rule = params["appraisal"].get(interaction["type"])
    if rule is None:
        return AppraisalResult(state=state, significant=False, reason="")

    payload = interaction["payload"]
    intensity = min(1.0, rule["base_intensity"] * _sensitivity(big_five, params))
    instance = EmotionInstance(
        type=rule["type"],
        intensity=round(intensity, 4),
        target_id=payload["player_id"],
        source_event=interaction["event_id"],
    )
    inst_params = params["instances"]
    emotions = merge_instance(
        state.emotions,
        instance,
        reinforcement=inst_params["reinforcement"],
        max_active=inst_params["max_active"],
    )

    # 감정 인스턴스 → mood 가중 반영
    weight = params["mood"]["instance_weight"] * intensity
    pad = rule["pad"]
    mood = Pad(
        pleasure=state.mood.pleasure + weight * pad["pleasure"],
        arousal=state.mood.arousal + weight * pad["arousal"],
        dominance=state.mood.dominance + weight * pad["dominance"],
    ).clamped()

    thresholds = params["shift_thresholds"]
    significant = (
        mood.l1_distance(state.mood) >= thresholds["mood_delta"]
        or intensity >= thresholds["instance_intensity"]
    )
    return AppraisalResult(
        state=EmotionState(mood=mood, emotions=emotions),
        significant=significant,
        reason=f"{interaction['type']} — {rule['type']} {intensity:.2f} "
        f"(플레이어 {payload['player_id']})",
    )


def decay(
    state: EmotionState,
    big_five: dict[str, float],
    ticks: int,
    *,
    params: dict[str, Any] | None = None,
) -> EmotionState:
    """tick 경과에 따른 감쇠 — 인스턴스는 지수 감쇠, mood는 baseline 회귀 (ADR-015).

    낙천가(낮은 neuroticism)는 더 빨리 회복한다 (resilience).
    """
    if ticks <= 0:
        return state
    params = params or default_params()
    resilience = _resilience(big_five, params)

    inst_factor = (1 - params["instances"]["decay_per_tick"] * resilience) ** ticks
    cull = params["instances"]["cull_threshold"]
    emotions = tuple(
        EmotionInstance(
            type=inst.type,
            intensity=round(inst.intensity * inst_factor, 4),
            target_id=inst.target_id,
            source_event=inst.source_event,
        )
        for inst in state.emotions
        if inst.intensity * inst_factor >= cull
    )

    baseline = baseline_from_ocean(big_five)
    regression = min(1.0, params["mood"]["regression_per_tick"] * resilience * ticks)
    mood = Pad(
        pleasure=state.mood.pleasure + (baseline.pleasure - state.mood.pleasure) * regression,
        arousal=state.mood.arousal + (baseline.arousal - state.mood.arousal) * regression,
        dominance=state.mood.dominance + (baseline.dominance - state.mood.dominance) * regression,
    ).clamped()
    return EmotionState(mood=mood, emotions=emotions)


def describe(state: EmotionState) -> str:
    """감정 상태 → 자연어 한 줄 (Context Fabric의 Working 섹션 주입용, ADR-015 §행동 연결)."""
    mood = state.mood
    tone = "밝은" if mood.pleasure > 0.15 else "가라앉은" if mood.pleasure < -0.15 else "담담한"
    energy = "들뜬" if mood.arousal > 0.2 else "지친" if mood.arousal < -0.2 else "차분한"
    parts = [f"지금 기분: {tone}, {energy} 상태"]
    for inst in state.top_emotions(2):
        target = f" ({inst.target_id} 때문)" if inst.target_id else ""
        parts.append(f"{inst.type} {inst.intensity:.1f}{target}")
    return " · ".join(parts)
