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
    #: 사람이 읽는 원인 한 줄 (이벤트 payload의 reason) — 아래 reason 계약 참조
    reason: str


# ── reason 계약 — 사람 문장 (발원지 정화) ─────────────────────────────────
# reason은 리시트(projector 타임라인)·이야기 사슬(feed-api story)·LLM 컨텍스트에
# 그대로 실리는 화면 문장이다. 계약: 이벤트 타입 토큰·수치·원시 id 금지,
# 결정적(같은 입력 → 같은 문장 — 리플레이 원칙). 판정용 구조 정보(감정 코드·
# 강도·대상)는 payload.emotions[]와 봉투 causation_id가 이미 나른다 —
# reason은 사람 몫, 판정은 구조 필드 몫 (이원화).

#: 감정 코드 → 한국어 어휘(주격 조사 포함) — projector 리시트·feed-api 이야기
#: 사슬의 한국어 감정 어휘와 같은 결. params가 만들 수 있는 전 코드를 덮는다.
_EMOTION_WORDS: dict[str, str] = {
    "joy": "기쁨이",
    "gratitude": "고마움이",
    "distress": "괴로움이",
    "anger": "화가",
}

#: 미지 감정 코드의 폴백 — params에 새 유형이 늘어도 문장은 사람 말로 남는다
_FALLBACK_EMOTION_WORD = "낯선 감정이"

#: 상호작용 타입 → 원인 구절 — 이벤트 타입 토큰을 화면 문장에 싣지 않는다
_INTERACTION_PHRASES: dict[str, str] = {
    "player.reaction.added": "좋아요에",
    "player.comment.posted": "댓글 한 마디에",
    "player.dm.sent": "DM 한 통에",
}

_FALLBACK_INTERACTION_PHRASE = "건네받은 마음에"

#: 목표 결과 → 문장 틀 ({word} 자리에 감정 어휘) — 완주는 그 자체로 큰 문장이다
_GOAL_TEMPLATES: dict[str, str] = {
    "goal.advanced": "하려던 일이 한 걸음 나아가 {word} 번졌다",
    "goal.achieved": "마음먹은 일을 이뤄내 {word} 크게 차올랐다",
    "goal.frustrated": "하려던 일이 막혀 {word} 밀려왔다",
}

#: 피드 글 감흥 채널 → 문장 틀 — 작성자는 구조 필드(emotions[].target_id)의 몫이다
_POST_TEMPLATES: dict[str, str] = {
    "warm": "아끼는 사람의 글에 {word} 잔잔히 번졌다",
    "sore": "마음 불편한 상대의 글에 {word} 일었다",
}

#: '크게'가 붙는 강도 문턱 — 수치는 문장에 싣지 않고 세기로만 남긴다 (남발 금지)
_STRONG_INTENSITY = 0.7


def _emotion_word(emotion_type: str) -> str:
    return _EMOTION_WORDS.get(emotion_type, _FALLBACK_EMOTION_WORD)


def _interaction_reason(interaction_type: str, emotion_type: str, intensity: float) -> str:
    phrase = _INTERACTION_PHRASES.get(interaction_type, _FALLBACK_INTERACTION_PHRASE)
    strength = "크게 " if intensity >= _STRONG_INTENSITY else ""
    return f"{phrase} {_emotion_word(emotion_type)} {strength}번졌다"


def _goal_reason(kind: str, emotion_type: str) -> str:
    template = _GOAL_TEMPLATES.get(kind, "{word} 마음에 번졌다")
    return template.format(word=_emotion_word(emotion_type))


def _post_reason(channel: str, emotion_type: str) -> str:
    template = _POST_TEMPLATES.get(channel, "그 사람의 글에 {word} 번졌다")
    return template.format(word=_emotion_word(emotion_type))


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
        reason=_interaction_reason(interaction["type"], rule["type"], intensity),
    )


def appraise_goal(
    state: EmotionState,
    big_five: dict[str, float],
    *,
    kind: str,
    magnitude: float,
    source_event: str | None = None,
    params: dict[str, Any] | None = None,
) -> AppraisalResult:
    """목표 결과를 평가한다 (ADR-015 goal_congruence) — 진전은 기쁨, 좌절은 괴로움.

    kind는 params.appraisal의 규칙 키 (goal.advanced / goal.frustrated).
    magnitude(정렬도 또는 결핍, 0..1)가 base_intensity를 스케일한다. 대상 없는
    감정이다 (사람이 아니라 자기 드라이브에 대한 것) — 관계로 스며들지 않는다.

    유의성은 mood 변화만으로 본다: 지속 조건(결핍)이 매 tick 재평가돼도 mood가
    이미 그쪽으로 기울면 더는 발행하지 않는다 (상호작용처럼 강도로 트리거하면 스팸).
    """
    params = params or default_params()
    rule = params["appraisal"].get(kind)
    if rule is None:
        return AppraisalResult(state=state, significant=False, reason="")

    intensity = min(1.0, rule["base_intensity"] * magnitude * _sensitivity(big_five, params))
    if intensity < params["instances"]["cull_threshold"]:
        return AppraisalResult(state=state, significant=False, reason="")

    instance = EmotionInstance(
        type=rule["type"], intensity=round(intensity, 4), target_id=None, source_event=source_event
    )
    inst_params = params["instances"]
    emotions = merge_instance(
        state.emotions, instance,
        reinforcement=inst_params["reinforcement"], max_active=inst_params["max_active"],
    )
    weight = params["mood"]["instance_weight"] * intensity
    pad = rule["pad"]
    mood = Pad(
        pleasure=state.mood.pleasure + weight * pad["pleasure"],
        arousal=state.mood.arousal + weight * pad["arousal"],
        dominance=state.mood.dominance + weight * pad["dominance"],
    ).clamped()

    significant = mood.l1_distance(state.mood) >= params["shift_thresholds"]["mood_delta"]
    return AppraisalResult(
        state=EmotionState(mood=mood, emotions=emotions),
        significant=significant,
        reason=_goal_reason(kind, rule["type"]),
    )


def appraise_post(
    state: EmotionState,
    big_five: dict[str, float],
    *,
    author_id: str,
    drama: float,
    edge: dict[str, float] | None,
    source_event: str | None = None,
    params: dict[str, Any] | None = None,
) -> AppraisalResult:
    """피드에서 본 남의 글을 평가한다 (액터 소셜 루프 — ADR-015 확장).

    관계의 온도가 감흥의 채널을 정한다: 앙금(resentment)이 온기(trust·intimacy)보다
    크면 불쾌, 아니면 잔잔한 기쁨. magnitude는 drama와 관계 강도의 곱 —
    엣지가 없거나 온도가 0이면 아무 일도 없다 (모르는 사람의 글이다).
    """
    params = params or default_params()
    if edge is None:
        return AppraisalResult(state=state, significant=False, reason="")

    rules = params["post_appraisal"]
    warmth = max(0.0, (edge.get("trust", 0.0) + edge.get("intimacy", 0.0)) / 2)
    sore = max(0.0, edge.get("resentment", 0.0))
    channel, strength = ("sore", sore) if sore > warmth else ("warm", warmth)
    if strength <= 0.0:
        return AppraisalResult(state=state, significant=False, reason="")

    rule = rules[channel]
    floor = float(rules["drama_floor"])
    magnitude = (floor + (1 - floor) * min(1.0, max(0.0, drama))) * strength
    intensity = min(1.0, rule["base_intensity"] * magnitude * _sensitivity(big_five, params))
    if intensity < params["instances"]["cull_threshold"]:
        return AppraisalResult(state=state, significant=False, reason="")

    instance = EmotionInstance(
        type=rule["type"],
        intensity=round(intensity, 4),
        target_id=author_id,
        source_event=source_event,
    )
    inst_params = params["instances"]
    emotions = merge_instance(
        state.emotions, instance,
        reinforcement=inst_params["reinforcement"], max_active=inst_params["max_active"],
    )
    weight = params["mood"]["instance_weight"] * intensity
    pad = rule["pad"]
    mood = Pad(
        pleasure=state.mood.pleasure + weight * pad["pleasure"],
        arousal=state.mood.arousal + weight * pad["arousal"],
        dominance=state.mood.dominance + weight * pad["dominance"],
    ).clamped()

    # 유의성은 mood 변화만으로 본다 (appraise_goal 규약) — 잔잔한 감흥이 강도
    # 트리거로 스팸이 되지 않게. 임계 미만은 상태만 물들고 이벤트는 안 남는다.
    significant = mood.l1_distance(state.mood) >= params["shift_thresholds"]["mood_delta"]
    return AppraisalResult(
        state=EmotionState(mood=mood, emotions=emotions),
        significant=significant,
        reason=_post_reason(channel, rule["type"]),
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
