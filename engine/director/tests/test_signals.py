"""서사 신호 순수 로직 검증 — 결정성·침체 감지·시선 추적 (ADR-013 §관찰)."""

from lf_director.signals import (
    AttentionTracker,
    DramaWindow,
    default_params,
    drama_contribution,
)


def player_hit(tick: int, target: str, kind: str = "player.dm.sent") -> dict:
    return {"type": kind, "tick": tick, "payload": {"target_actor_id": target}}


def test_attention_tracker_counts_within_window():
    """Narrative Gravity — 창 안의 플레이어 시선만 센다 (어제까지가 오늘의 무대)."""
    tracker = AttentionTracker(window_ticks=100)
    tracker.observe(player_hit(10, "a_minji"))
    tracker.observe(player_hit(20, "a_minji", "player.reaction.added"))
    tracker.observe(player_hit(30, "a_seongho", "player.comment.posted"))
    tracker.observe({"type": "actor.action.performed", "tick": 30,
                     "payload": {"action_kind": "work"}})  # 시선이 아니다 — 무시
    assert tracker.scores(50) == {"a_minji": 2, "a_seongho": 1}
    assert tracker.top(50) == [("a_minji", 2), ("a_seongho", 1)]
    # 창(100 tick)을 벗어난 시선은 잊힌다 — tick 125에서 10·20은 창 밖, 30은 안
    assert tracker.scores(125) == {"a_seongho": 1}
    assert tracker.scores(200) == {}


def test_attention_tracker_breaks_ties_deterministically():
    tracker = AttentionTracker(window_ticks=100)
    tracker.observe(player_hit(10, "a_b"))
    tracker.observe(player_hit(10, "a_a"))
    assert tracker.top(20) == [("a_a", 1), ("a_b", 1)]  # 동률은 id 순 — 리플레이 재현


def action(kind: str, target: str | None = None) -> dict:
    return {
        "type": "actor.action.performed",
        "payload": {"action_kind": kind, "target_actor_id": target},
    }


def test_contribution_orders_conflict_over_routine():
    confront = drama_contribution(action("confront", "a_b"))
    speak = drama_contribution(action("speak"))
    rest = drama_contribution(action("rest"))
    assert confront > speak > rest
    # 대인 사건 증폭
    assert drama_contribution(action("speak", "a_b")) > speak


def test_contribution_reads_emotion_and_feed():
    shift = {
        "type": "actor.emotion.shifted",
        "payload": {"emotions": [{"type": "anger", "intensity": 0.8, "target_id": "a_b"}]},
    }
    post = {"type": "feed.post.published", "payload": {"drama_score": 0.9}}
    unknown = {"type": "system.tick.started", "payload": {}}
    assert drama_contribution(shift) > 0
    assert drama_contribution(post) > 0
    assert drama_contribution(unknown) == 0.0


def test_quiet_ticks_accumulate_and_reset_on_drama():
    window = DramaWindow()
    threshold = default_params()["observation"]["quiet_threshold"]

    for tick in range(5):  # 아무 일 없는 tick들
        snapshot = window.close_tick(tick)
    assert snapshot.quiet_ticks == 5
    assert snapshot.drama_ma < threshold

    # 큰 사건이 이동평균을 임계 위로 끌어올리면 침체가 풀린다
    for _ in range(20):
        window.observe(action("confront", "a_b"))
    snapshot = window.close_tick(5)
    assert snapshot.quiet_ticks == 0
    assert snapshot.drama_ma >= threshold


def test_reset_quiet_after_intervention():
    window = DramaWindow()
    for tick in range(40):
        window.close_tick(tick)
    window.reset_quiet()
    assert window.close_tick(40).quiet_ticks == 1  # 다시 처음부터 센다
