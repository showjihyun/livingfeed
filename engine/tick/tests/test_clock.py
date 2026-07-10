from datetime import datetime, timedelta, timezone

import pytest

from lf_tick.clock import TickClock

GENESIS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_one_tick_is_four_world_minutes() -> None:
    clock = TickClock(genesis=GENESIS)
    assert clock.world_time_at(1) - GENESIS == timedelta(minutes=4)


def test_one_world_day_is_360_ticks() -> None:
    # ADR-011: 현실 6시간 = 세계 하루 → 60초/tick 기준 360 tick
    clock = TickClock(genesis=GENESIS)
    assert clock.world_days_elapsed(360) == 1.0


def test_roundtrip() -> None:
    clock = TickClock(genesis=GENESIS)
    assert clock.tick_at(clock.world_time_at(182_734)) == 182_734


def test_naive_genesis_rejected() -> None:
    with pytest.raises(ValueError):
        TickClock(genesis=datetime(2026, 1, 1))


def test_negative_tick_rejected() -> None:
    with pytest.raises(ValueError):
        TickClock(genesis=GENESIS).world_time_at(-1)
