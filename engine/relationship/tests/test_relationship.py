"""Relationship Engine 순수 로직 검증 — 응고·비대칭·감쇠·발행 규칙 (ADR-016)."""

import re

from lf_relationship import (
    RelationshipState,
    apply_interaction,
    consolidate_emotion,
    consolidate_insight,
    consume_pending,
    decay,
    default_params,
    describe_edges,
    stage_after_action,
    transition_stage,
)
from lf_relationship import engine as engine_module


def test_help_builds_trust_asymmetrically():
    # A가 B를 도왔다: B→A(incoming)는 신뢰가 크게, A→B(outgoing)는 친밀이 조금
    receiver = apply_interaction(RelationshipState(), "action.help", "incoming")
    giver = apply_interaction(RelationshipState(), "action.help", "outgoing")
    assert receiver.state.dimensions["trust"] > giver.state.dimensions["trust"]
    assert giver.state.dimensions["intimacy"] > 0


def test_confront_raises_resentment_only_for_target():
    target = apply_interaction(RelationshipState(), "action.confront", "incoming")
    actor = apply_interaction(RelationshipState(), "action.confront", "outgoing")
    assert target.state.dimensions["resentment"] > 0
    assert target.state.dimensions["trust"] < 0
    assert actor.state.dimensions["resentment"] == 0  # 비대칭 — 내가 따진 건 원한이 아니다


def test_repeated_anger_accumulates_resentment():
    """반복 anger → resentment 누적 (ADR-016 핵심 서사 장치)."""
    state = RelationshipState()
    for _ in range(5):
        state = consolidate_emotion(state, "anger", 0.7).state
    assert state.dimensions["resentment"] > 0.2
    assert state.dimensions["trust"] < -0.1

    # 화해(신뢰 회복)가 있어도 resentment는 독립 축으로 잔류한다
    recovered = consolidate_emotion(state, "gratitude", 0.9).state
    assert recovered.dimensions["trust"] > state.dimensions["trust"]
    assert recovered.dimensions["resentment"] == state.dimensions["resentment"]


def test_insight_raises_salience_without_touching_dimensions():
    """인물 통찰 응고 — 비중(salience)만 자란다, 차원은 그대로 (ADR-016/008).

    생각만으로 마음(신뢰·원한)이 바뀌진 않지만, 그 사람이 삶에서 차지하는
    자리는 는다. 발행 대상도 아니다 — 조용한 내면 변화다.
    """
    state = RelationshipState()
    result = consolidate_insight(state, 0.9)
    assert result.publish is False
    assert result.state.salience == default_params()["insight_salience"] * 0.9
    assert result.state.dimensions == state.dimensions  # 차원 불변
    assert result.state.pending == state.pending  # 발행 누적에도 안 들어간다

    # 상한 1.0 클램프 + 확신 0은 무변화
    high = RelationshipState(
        dimensions=state.dimensions, stage=state.stage, salience=0.99, pending=state.pending
    )
    assert consolidate_insight(high, 1.0).state.salience == 1.0
    assert consolidate_insight(state, 0.0).state is state


def test_publish_threshold_accumulates_until_crossed():
    params = default_params()
    state = RelationshipState()
    published = False
    # 좋아요(미세 델타)는 한 번으론 침묵, 누적되면 반드시 발행 — 스팸도 침묵도 없다
    for _ in range(30):
        result = apply_interaction(state, "player.reaction.added", "incoming", params=params)
        state = result.state
        if result.publish:
            published = True
            break
    assert published
    cleared, deltas = consume_pending(state)
    assert sum(abs(v) for v in deltas.values()) >= params["publish_threshold"]
    assert cleared.pending_l1() == 0.0
    assert cleared.dimensions == state.dimensions  # 발행은 상태를 바꾸지 않는다


def test_decay_cools_intimacy_but_resentment_lingers():
    state = apply_interaction(RelationshipState(), "action.help", "incoming").state
    state = consolidate_emotion(state, "anger", 0.8).state
    later = decay(state, ticks=1000)
    assert later.dimensions["intimacy"] < state.dimensions["intimacy"]
    assert later.salience < state.salience
    # 1000 tick(~세계 17시간)이 지나도 원한은 거의 그대로다
    assert later.dimensions["resentment"] > state.dimensions["resentment"] * 0.95


def test_stage_transition_is_event_driven_not_automatic():
    state = RelationshipState()
    for _ in range(50):  # 수치가 아무리 차도
        state = apply_interaction(state, "player.dm.sent", "incoming").state
    assert state.stage == "stranger"  # 자동 전이는 없다 (ADR-016)
    assert transition_stage(state, "acquaintance").stage == "acquaintance"


def test_unknown_source_is_neutral():
    result = apply_interaction(RelationshipState(), "action.juggle", "incoming")
    assert result.state == RelationshipState()
    assert not result.publish


def test_state_roundtrips_json():
    state = apply_interaction(RelationshipState(), "action.help", "incoming").state
    canonical = state.to_json()
    assert RelationshipState.from_json(canonical).to_json() == canonical


# --- reason 계약 — 사람 문장 (발원지 정화) ------------------------------------
# reason은 리시트(projector 타임라인)·이야기 사슬(feed-api)·LLM 컨텍스트에 그대로
# 실린다. 계약: 이벤트 타입 토큰·방향 표기·수치·원시 id 금지, 결정적.

_TYPE_TOKEN = re.compile(r"[a-z_]+\.[a-z_.]+")  # 점 표기 기계 토큰 (player.dm.sent 등)
_RAW_ID = re.compile(r"\b[apw]_[A-Za-z0-9_]+")  # 원시 식별자 (p_/a_/w_ 접두)


def _assert_human(reason: str) -> None:
    assert reason, "발행되는 변화의 reason은 빈 문장일 수 없다"
    assert not re.search(r"\d", reason), f"수치 유출: {reason!r}"
    assert _TYPE_TOKEN.search(reason) is None, f"타입 토큰 유출: {reason!r}"
    assert _RAW_ID.search(reason) is None, f"원시 id 유출: {reason!r}"
    for token in ("incoming", "outgoing", "received", "sent"):
        assert token not in reason, f"방향 표기 유출: {reason!r}"


def test_interaction_reason_is_human_sentence():
    result = apply_interaction(RelationshipState(), "player.comment.posted", "incoming")
    assert result.reason == "댓글 한 마디를 받았다"
    helped = apply_interaction(RelationshipState(), "action.help", "outgoing")
    assert helped.reason == "손을 내밀어 도왔다"
    # 결정성 — 같은 입력, 같은 문장 (리플레이 재현성)
    assert result.reason == apply_interaction(
        RelationshipState(), "player.comment.posted", "incoming"
    ).reason


def test_interaction_reasons_are_human_for_all_configured_kinds():
    """params의 모든 (사건, 방향) 조합이 사람 문장을 얻는다 — 기계 표기 전수 검사."""
    params = default_params()
    for kind, directions in params["interaction_effects"].items():
        for direction in directions:
            result = apply_interaction(RelationshipState(), kind, direction, params=params)
            _assert_human(result.reason)
            assert kind not in result.reason


def test_novel_interaction_kind_falls_back_to_human_sentence():
    """params에 새 사건이 늘어도 reason은 사람 문장으로 남는다 (전방 호환)."""
    params = {
        **default_params(),
        "interaction_effects": {"action.hug": {"incoming": {"intimacy": 0.05}}},
    }
    result = apply_interaction(RelationshipState(), "action.hug", "incoming", params=params)
    _assert_human(result.reason)
    assert "action.hug" not in result.reason


def test_consolidation_reason_is_human_sentence():
    result = consolidate_emotion(RelationshipState(), "gratitude", 0.5)
    assert result.reason == "그 사람과의 사이에 고마움이 쌓였다"
    _assert_human(result.reason)
    # 강도는 수치가 아니라 문장의 세기로만 — 임계(0.7) 이상만 '깊이'
    strong = consolidate_emotion(RelationshipState(), "anger", 0.8)
    assert strong.reason == "그 사람과의 사이에 노여움이 깊이 쌓였다"
    _assert_human(strong.reason)
    assert "깊이" not in consolidate_emotion(RelationshipState(), "anger", 0.3).reason


def test_consolidation_vocab_covers_all_configured_emotions():
    """emotion_consolidation의 전 감정 코드에 한국어 어휘가 있다 — 폴백은 미지 코드 전용."""
    params = default_params()
    assert set(params["emotion_consolidation"]) <= set(engine_module._EMOTION_WORDS)
    for code in params["emotion_consolidation"]:
        result = consolidate_emotion(RelationshipState(), code, 0.6, params=params)
        _assert_human(result.reason)
        assert code not in result.reason  # 감정 코드는 구조 필드의 몫, 문장엔 어휘만


def test_insight_reason_is_human_sentence():
    _assert_human(consolidate_insight(RelationshipState(), 0.9).reason)


def _edge(
    trust=0.0, intimacy=0.0, resentment=0.0, respect=0.0, attraction=0.0,
    salience=0.0, stage="stranger",
) -> RelationshipState:
    base = RelationshipState()
    return RelationshipState(
        dimensions={
            **base.dimensions,
            "trust": trust, "intimacy": intimacy, "resentment": resentment,
            "respect": respect, "attraction": attraction,
        },
        stage=stage, salience=salience, pending=base.pending,
    )


def test_describe_edges_picks_top_salience_deterministically():
    """비중(salience) 상위만 접힌다 — 삶에서 자리가 큰 관계부터 (ADR-009 §3)."""
    edges = {f"a_{i}": _edge(salience=i / 10) for i in range(5)}
    text = describe_edges(edges, limit=3)
    assert text is not None
    lines = text.splitlines()
    assert len(lines) == 3
    assert "a_4" in lines[0] and "a_3" in lines[1] and "a_2" in lines[2]
    assert "a_0" not in text  # 비중 낮은 관계는 접힌다
    assert describe_edges(edges, limit=3) == text  # 결정적


def test_describe_edges_love_and_hate_coexist():
    """애증 공존 — trust·intimacy와 resentment는 독립 축 (ADR-016), 둘 다면 둘 다."""
    text = describe_edges({"a_x": _edge(trust=0.5, intimacy=0.4, resentment=0.5)})
    assert text is not None
    assert "믿고 가까운" in text and "앙금" in text

    # 단독 결은 단독으로만
    close_only = describe_edges({"a_x": _edge(trust=0.5, intimacy=0.4)})
    assert "믿고 가까운" in close_only and "앙금" not in close_only
    grudge_only = describe_edges({"a_x": _edge(resentment=0.5)})
    assert "앙금" in grudge_only and "믿고 가까운" not in grudge_only


def test_describe_edges_respect_texture():
    """존중 결 — respect 임계(0.25)를 넘으면 한 수 접어주는 감각이 스민다."""
    text = describe_edges({"a_x": _edge(respect=0.4)})
    assert text == "- a_x: 한 수 접어주게 되는 상대다"


def test_describe_edges_attraction_texture():
    """끌림 결 — attraction 임계(0.25)를 넘으면 자꾸 눈이 가는 감각이 스민다."""
    text = describe_edges({"a_x": _edge(attraction=0.4)})
    assert text == "- a_x: 자꾸 눈이 가는 사람이다"


def test_describe_edges_attraction_and_grudge_coexist():
    """끌림+앙금 공존 — 독립 축(ADR-016), 애증처럼 둘 다 말한다. 긴장이 뒷절."""
    text = describe_edges({"a_x": _edge(attraction=0.5, resentment=0.5)})
    assert text == "- a_x: 자꾸 눈이 가는 사람이지만, 앙금도 남아 있다"


def test_describe_edges_caps_at_two_textures():
    """최대 2결 — 우선순위 긴장 > 끌림 > 존중 > 친밀·신뢰, 나머지는 접힌다."""
    text = describe_edges(
        {"a_x": _edge(trust=0.5, intimacy=0.4, respect=0.5, attraction=0.5, resentment=0.5)}
    )
    assert "앙금" in text and "자꾸 눈이" in text  # 긴장 + 끌림만 살아남는다
    assert "한 수" not in text and "믿고 가까운" not in text


def test_describe_edges_labels_named_stage():
    """기본 아닌 stage는 관계 라벨을 이름 뒤에 — 라이벌의 앙금은 낯선 이의 앙금과 다르다."""
    edges = {"a_junho_park": _edge(resentment=0.4, stage="rival")}
    text = describe_edges(edges, {"a_junho_park": "박준호"})
    assert text is not None
    assert text.startswith("- 박준호(라이벌): 앙금")


def test_describe_edges_omits_default_stage_label():
    """기본 stage(stranger/acquaintance)는 라벨 생략 — 현행 유지."""
    for stage in ("stranger", "acquaintance"):
        text = describe_edges({"a_x": _edge(trust=0.5, intimacy=0.4, stage=stage)})
        assert text == "- a_x: 믿고 가까운 사이다"


def test_describe_edges_grounds_names_via_map():
    edges = {"a_junho_park": _edge(resentment=0.4, salience=0.5)}
    text = describe_edges(edges, {"a_junho_park": "박준호"})
    assert "박준호" in text and "a_junho_park" not in text
    # name_map에 없는 상대(플레이어 등)는 id 그대로 — 기존 관례
    assert "a_junho_park" in describe_edges(edges)


def test_describe_edges_empty_returns_none():
    assert describe_edges({}) is None  # 섹션 생략 규약 — 빈 관계는 소음이다


def test_describe_edges_contempt_texture():
    """경멸 결 — respect 음극(-0.25 이하)이면 얕보는 감각이 스민다 (양극 축의 반대쪽)."""
    assert describe_edges({"a_x": _edge(respect=-0.3)}) == "- a_x: 어딘가 얕보게 되는 상대다"
    # 온기(끌림)와 공존하면 긴장은 뒷절 — 대비의 방향이 서사다
    both = describe_edges({"a_x": _edge(attraction=0.5, respect=-0.5)})
    assert both == "- a_x: 자꾸 눈이 가는 사람이지만, 어딘가 얕보게 되는 상대다"


def test_describe_edges_contempt_folds_under_grudge_and_distrust():
    """긴장 결 우선순위 — 앙금 > 불신 > 경멸: 더 뜨거운 긴장이 있으면 접힌다."""
    grudge = describe_edges({"a_x": _edge(resentment=0.5, respect=-0.5)})
    assert "앙금" in grudge and "얕보" not in grudge
    distrust = describe_edges({"a_x": _edge(trust=-0.5, respect=-0.5)})
    assert "믿기 어려운" in distrust and "얕보" not in distrust


def test_stage_after_action_confess_acceptance_boundary():
    """고백 수락 경계 — 대상→행위자 엣지의 trust ≥ 0.2 ∧ resentment < 0.3 (ADR-016)."""
    me = _edge(stage="acquaintance")
    accepted = stage_after_action("confess", me, _edge(trust=0.2, resentment=0.29))
    assert accepted is not None
    assert (accepted.actor_stage, accepted.target_stage) == ("romantic", "romantic")
    assert accepted.milestone == "confession_accepted"
    assert len(accepted.note) <= 200  # 스키마 note 상한

    # 신뢰가 모자라거나 앙금이 답을 막으면 거절 — stage 불변이지만 마일스톤은 남는다
    for other in (_edge(trust=0.19), _edge(trust=0.5, resentment=0.3)):
        declined = stage_after_action("confess", me, other)
        assert declined is not None
        assert (declined.actor_stage, declined.target_stage) == (None, None)
        assert declined.milestone == "confession_declined"
        assert len(declined.note) <= 200


def test_stage_after_action_sever_returns_both_to_stranger():
    """절교 — 조건 없이 양쪽 stranger 회귀. 끊는 데는 상대의 동의가 필요 없다."""
    result = stage_after_action("sever", _edge(stage="friend"), _edge(stage="close_friend"))
    assert result is not None
    assert (result.actor_stage, result.target_stage) == ("stranger", "stranger")
    assert result.milestone == "severed"


def test_stage_after_action_repeat_is_noop():
    """재고백·재절교는 마일스톤이 아니다 — 이미 그 관계면 None."""
    romantic = _edge(trust=0.5, stage="romantic")
    assert stage_after_action("confess", romantic, romantic) is None
    stranger = _edge(stage="stranger")
    assert stage_after_action("sever", stranger, stranger) is None
    # 한쪽만 그 stage면 전이는 여전히 일어난다 (엇갈린 상태의 정합 회복)
    assert stage_after_action("confess", romantic, _edge(trust=0.5)) is not None
    assert stage_after_action("sever", stranger, _edge(stage="friend")) is not None


def test_stage_after_action_other_kinds_are_none():
    """그 외 행동은 stage를 못 바꾼다 — 전이는 오직 상위 전이 행동으로 (ADR-016)."""
    assert stage_after_action("speak", _edge(trust=0.9), _edge(trust=0.9)) is None
    assert stage_after_action("confront", _edge(), _edge()) is None
