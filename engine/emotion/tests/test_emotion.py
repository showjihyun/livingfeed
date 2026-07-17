"""Emotion Engine 순수 로직 검증 — 결정성·개인차·감쇠 (ADR-015)."""

from lf_emotion import (
    EmotionState,
    appraise_goal,
    appraise_interaction,
    appraise_post,
    baseline_from_ocean,
    decay,
    describe,
)

OPTIMIST = {  # 낮은 신경성, 높은 외향 — 밝고 회복 빠름
    "openness": 0.6, "conscientiousness": 0.6, "extraversion": 0.8,
    "agreeableness": 0.7, "neuroticism": 0.2,
}
NEUROTIC = {  # 높은 신경성 — 예민하고 회복 느림
    "openness": 0.6, "conscientiousness": 0.6, "extraversion": 0.4,
    "agreeableness": 0.5, "neuroticism": 0.9,
}


def dm(event_id: str = "01JZK7Q3W0000000000000000G") -> dict:
    return {
        "event_id": event_id,
        "type": "player.dm.sent",
        "payload": {"player_id": "p_observer_0417", "target_actor_id": "a_x", "text": "응원해요"},
    }


def test_baseline_reflects_personality():
    sunny = baseline_from_ocean(OPTIMIST)
    gloomy = baseline_from_ocean(NEUROTIC)
    assert sunny.pleasure > gloomy.pleasure  # 낙천가의 기본 기분이 더 밝다
    assert gloomy.arousal > sunny.arousal  # 신경성은 각성 기준선을 올린다


def test_appraisal_is_deterministic_and_targeted():
    a = appraise_interaction(EmotionState(), dm(), OPTIMIST)
    b = appraise_interaction(EmotionState(), dm(), OPTIMIST)
    assert a == b  # 리플레이 재현성 (ADR-015 요구 1)

    [instance] = a.state.emotions
    assert instance.type == "gratitude"
    assert instance.target_id == "p_observer_0417"  # '누구 때문에'가 남는다
    assert instance.source_event == dm()["event_id"]
    assert a.significant  # DM 강도는 발행 임계 이상
    assert a.state.mood.pleasure > 0


def test_same_event_different_person():
    sensitive = appraise_interaction(EmotionState(), dm(), NEUROTIC)
    steady = appraise_interaction(EmotionState(), dm(), OPTIMIST)
    # 신경성 민감도 vs 외향 사회 증폭 — 어느 쪽이든 개인차가 존재해야 한다
    assert sensitive.state.emotions[0].intensity != steady.state.emotions[0].intensity


def test_repeated_interaction_reinforces_not_duplicates():
    first = appraise_interaction(EmotionState(), dm("01JZK7Q3W0000000000000000G"), OPTIMIST)
    second = appraise_interaction(first.state, dm("01JZK7Q3W0000000000000000H"), OPTIMIST)
    assert len(second.state.emotions) == 1  # 같은 (type, target)은 강화
    assert second.state.emotions[0].intensity > first.state.emotions[0].intensity


def test_unknown_event_type_is_neutral():
    result = appraise_interaction(
        EmotionState(), {"event_id": "x", "type": "player.unknown.thing", "payload": {}}, OPTIMIST
    )
    assert result.state == EmotionState()
    assert not result.significant


def test_decay_fades_instances_and_regresses_mood():
    shifted = appraise_interaction(EmotionState(), dm(), NEUROTIC).state
    later = decay(shifted, NEUROTIC, ticks=200)
    assert later.emotions == () or later.emotions[0].intensity < shifted.emotions[0].intensity
    baseline = baseline_from_ocean(NEUROTIC)
    # mood가 baseline 쪽으로 움직였다
    assert later.mood.l1_distance(baseline) <= shifted.mood.l1_distance(baseline)


def test_optimist_recovers_faster():
    sunny = appraise_interaction(EmotionState(), dm(), OPTIMIST).state
    gloomy = appraise_interaction(EmotionState(), dm(), NEUROTIC).state
    sunny_later = decay(sunny, OPTIMIST, ticks=100)
    gloomy_later = decay(gloomy, NEUROTIC, ticks=100)

    sunny_ratio = (sunny_later.emotions[0].intensity / sunny.emotions[0].intensity
                   if sunny_later.emotions else 0.0)
    gloomy_ratio = (gloomy_later.emotions[0].intensity / gloomy.emotions[0].intensity
                    if gloomy_later.emotions else 0.0)
    assert sunny_ratio < gloomy_ratio  # 낙천가의 감정이 더 빨리 사그라든다 (resilience)


def test_describe_is_human_readable():
    state = appraise_interaction(EmotionState(), dm(), OPTIMIST).state
    text = describe(state)
    assert "지금 기분" in text and "gratitude" in text


def test_state_roundtrips_json():
    # to_json은 4자리 라운딩된 정준형이다 — 왕복은 정준형 기준으로 안정적이어야 한다
    state = appraise_interaction(EmotionState(), dm(), OPTIMIST).state
    canonical = state.to_json()
    assert EmotionState.from_json(canonical).to_json() == canonical


def test_goal_advance_produces_joy_and_lifts_mood():
    result = appraise_goal(
        EmotionState(), OPTIMIST, kind="goal.advanced", magnitude=0.9, source_event="01J"
    )
    assert result.significant
    assert result.state.emotions[0].type == "joy"
    assert result.state.emotions[0].target_id is None  # 대상 없는 감정 (자기 드라이브)
    assert result.state.mood.pleasure > 0  # 기분이 밝아진다


def test_goal_frustration_produces_distress_and_drops_mood():
    result = appraise_goal(
        EmotionState(), NEUROTIC, kind="goal.frustrated", magnitude=0.8, source_event=None
    )
    assert result.significant
    assert result.state.emotions[0].type == "distress"
    assert result.state.mood.pleasure < 0  # 기분이 가라앉는다


def test_tiny_goal_magnitude_is_insignificant():
    # 미약한 정렬도는 감정을 흔들지 않는다 (mood-delta 유의성)
    result = appraise_goal(EmotionState(), OPTIMIST, kind="goal.advanced", magnitude=0.02)
    assert not result.significant


def test_unknown_goal_kind_is_noop():
    result = appraise_goal(EmotionState(), OPTIMIST, kind="goal.exploded", magnitude=1.0)
    assert not result.significant
    assert result.state == EmotionState()


# --- 피드 포스트 지각 → 감정 (액터 소셜 루프) ---------------------------------

WARM_EDGE = {"trust": 0.7, "intimacy": 0.8, "respect": 0.3, "attraction": 0.0,
             "resentment": 0.0}
SORE_EDGE = {"trust": -0.2, "intimacy": 0.1, "respect": 0.0, "attraction": 0.0,
             "resentment": 0.7}


def test_post_from_warm_relation_brings_small_joy():
    result = appraise_post(
        EmotionState(), OPTIMIST,
        author_id="a_friend", drama=0.8, edge=WARM_EDGE, source_event="01J",
    )
    [instance] = result.state.emotions
    assert instance.type == "joy"
    assert instance.target_id == "a_friend"  # '누구의 글 때문에'가 남는다
    assert result.state.mood.pleasure > 0
    # 결정성 — 같은 입력, 같은 결과 (리플레이 재현성)
    assert result == appraise_post(
        EmotionState(), OPTIMIST,
        author_id="a_friend", drama=0.8, edge=WARM_EDGE, source_event="01J",
    )


def test_post_from_resented_relation_brings_small_displeasure():
    result = appraise_post(
        EmotionState(), NEUROTIC,
        author_id="a_rival", drama=0.8, edge=SORE_EDGE, source_event="01J",
    )
    [instance] = result.state.emotions
    assert instance.type == "distress"
    assert instance.target_id == "a_rival"
    assert result.state.mood.pleasure < 0


def test_post_without_relation_leaves_no_trace():
    # 모르는 사람의 글은 마음을 흔들지 않는다 — 관계 강도가 magnitude의 축이다
    result = appraise_post(
        EmotionState(), OPTIMIST, author_id="a_stranger", drama=1.0, edge=None,
    )
    assert not result.significant
    assert result.state == EmotionState()


def test_post_magnitude_scales_with_drama_and_relation():
    calm = appraise_post(
        EmotionState(), OPTIMIST, author_id="a_friend", drama=0.1, edge=WARM_EDGE,
    )
    dramatic = appraise_post(
        EmotionState(), OPTIMIST, author_id="a_friend", drama=1.0, edge=WARM_EDGE,
    )
    assert dramatic.state.emotions[0].intensity > calm.state.emotions[0].intensity

    faint_edge = {**WARM_EDGE, "trust": 0.2, "intimacy": 0.2}
    faint = appraise_post(
        EmotionState(), OPTIMIST, author_id="a_friend", drama=1.0, edge=faint_edge,
    )
    assert dramatic.state.emotions[0].intensity > faint.state.emotions[0].intensity
