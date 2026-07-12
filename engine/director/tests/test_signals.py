"""서사 신호 순수 로직 검증 — 결정성·침체 감지 (ADR-013 §관찰)."""

from lf_director.signals import DramaWindow, default_params, drama_contribution


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
