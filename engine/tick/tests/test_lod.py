"""LOD 스케줄링 순수 로직 검증 (ADR-011 §액터 LOD 스케줄링)."""

from lf_tick.lod import (
    COLD_INTERVAL,
    HOT_DEMOTION_GRACE,
    WARM_DEMOTION_GRACE,
    WARM_INTERVAL,
    ActorLod,
    Tier,
    due_by_tier,
    is_due,
    maybe_demote,
    phase_offset,
    promote,
    scheduled_counts,
    touch,
)


def test_hot_is_due_every_tick():
    lod = ActorLod(tier=Tier.HOT, last_interest_tick=0)
    assert all(is_due("a_x", lod, t) for t in range(20))


def test_warm_is_due_once_per_interval():
    lod = ActorLod(tier=Tier.WARM, last_interest_tick=0)
    due_ticks = [t for t in range(WARM_INTERVAL * 3) if is_due("a_x", lod, t)]
    assert len(due_ticks) == 3
    assert due_ticks[1] - due_ticks[0] == WARM_INTERVAL


def test_cold_is_due_once_per_interval():
    lod = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    due_ticks = [t for t in range(COLD_INTERVAL * 2) if is_due("a_x", lod, t)]
    assert len(due_ticks) == 2


def test_phase_offset_is_deterministic_and_spread():
    assert phase_offset("a_mint", WARM_INTERVAL) == phase_offset("a_mint", WARM_INTERVAL)
    offsets = {phase_offset(f"a_{i}", WARM_INTERVAL) for i in range(200)}
    assert len(offsets) == WARM_INTERVAL  # 200명이면 10개 위상이 전부 채워진다


def test_promotion_is_immediate():
    cold = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    assert promote(cold, tick=42) == ActorLod(tier=Tier.HOT, last_interest_tick=42)


def test_demotion_hysteresis():
    hot = ActorLod(tier=Tier.HOT, last_interest_tick=100)
    # 유예 내에는 유지
    assert maybe_demote(hot, 100 + HOT_DEMOTION_GRACE - 1).tier is Tier.HOT
    # 유예가 지나면 한 단계만 강등
    warm = maybe_demote(hot, 100 + HOT_DEMOTION_GRACE)
    assert warm.tier is Tier.WARM
    assert maybe_demote(warm, 100 + WARM_DEMOTION_GRACE - 1).tier is Tier.WARM
    assert maybe_demote(warm, 100 + WARM_DEMOTION_GRACE).tier is Tier.COLD


def test_touch_resets_demotion_timer():
    hot = ActorLod(tier=Tier.HOT, last_interest_tick=0)
    touched = touch(hot, tick=HOT_DEMOTION_GRACE - 1)
    assert maybe_demote(touched, HOT_DEMOTION_GRACE + 5).tier is Tier.HOT


def test_due_by_tier_sorted_and_counted():
    lods = {
        "a_hot2": ActorLod(Tier.HOT, 0),
        "a_hot1": ActorLod(Tier.HOT, 0),
        "a_warm": ActorLod(Tier.WARM, 0),
    }
    warm_due_tick = phase_offset("a_warm", WARM_INTERVAL)
    due = due_by_tier(lods, warm_due_tick)
    assert due[Tier.HOT] == ["a_hot1", "a_hot2"]  # actor_id 순 — 결정적 (ADR-011)
    assert due[Tier.WARM] == ["a_warm"]

    counts = scheduled_counts(lods, warm_due_tick)
    assert counts == {"hot": 2, "warm": 1, "cold": 0}
