"""Relationship Engine 순수 로직 검증 — 응고·비대칭·감쇠·발행 규칙 (ADR-016)."""

from lf_relationship import (
    RelationshipState,
    apply_interaction,
    consolidate_emotion,
    consolidate_insight,
    consume_pending,
    decay,
    default_params,
    describe_edges,
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


def _edge(trust=0.0, intimacy=0.0, resentment=0.0, salience=0.0) -> RelationshipState:
    base = RelationshipState()
    return RelationshipState(
        dimensions={
            **base.dimensions,
            "trust": trust, "intimacy": intimacy, "resentment": resentment,
        },
        stage=base.stage, salience=salience, pending=base.pending,
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


def test_describe_edges_grounds_names_via_map():
    edges = {"a_junho_park": _edge(resentment=0.4, salience=0.5)}
    text = describe_edges(edges, {"a_junho_park": "박준호"})
    assert "박준호" in text and "a_junho_park" not in text
    # name_map에 없는 상대(플레이어 등)는 id 그대로 — 기존 관례
    assert "a_junho_park" in describe_edges(edges)


def test_describe_edges_empty_returns_none():
    assert describe_edges({}) is None  # 섹션 생략 규약 — 빈 관계는 소음이다
