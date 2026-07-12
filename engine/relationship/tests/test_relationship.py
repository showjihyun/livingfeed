"""Relationship Engine 순수 로직 검증 — 응고·비대칭·감쇠·발행 규칙 (ADR-016)."""

from lf_relationship import (
    RelationshipState,
    apply_interaction,
    consolidate_emotion,
    consume_pending,
    decay,
    default_params,
    transition_stage,
)


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
