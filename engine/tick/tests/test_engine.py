"""tick engine 통합 검증 — 실제 PostgreSQL 대상 (conftest 참고)."""

import asyncio
from datetime import UTC, datetime

from lf_eventstore import read_stream
from lf_tick.clock import TickClock
from lf_tick.config import TickConfig
from lf_tick.engine import (
    restore_position,
    run_tick,
    run_tick_loop,
    try_acquire_world_leadership,
)
from lf_tick.pipeline import NoopPhases
from psycopg import AsyncConnection

from .conftest import PG_DSN

WORLD = "w_test"
GENESIS = datetime(2026, 3, 1, tzinfo=UTC)
CLOCK = TickClock(genesis=GENESIS)


async def test_restore_position_empty_world(conn):
    assert await restore_position(conn, WORLD) == (0, 0)


async def test_run_tick_appends_started_and_completed(conn):
    head = await run_tick(conn, NoopPhases(), CLOCK, WORLD, tick=0, head=0)
    assert head == 2

    events = await read_stream(conn, WORLD, "system", "tick")
    started, completed = (e.envelope for e in events)
    assert started["type"] == "system.tick.started"
    assert completed["type"] == "system.tick.completed"
    # completed는 started에서 인과·상관이 이어진다 (ADR-002 규칙 5)
    assert completed["causation_id"] == started["event_id"]
    assert completed["correlation_id"] == started["correlation_id"]
    assert completed["payload"]["actors_decided"] == {"hot": 0, "warm": 0, "cold": 0}
    assert completed["payload"]["duration_ms"] >= 0


async def test_restore_position_continues_after_completed(conn):
    head = await run_tick(conn, NoopPhases(), CLOCK, WORLD, tick=0, head=0)
    head = await run_tick(conn, NoopPhases(), CLOCK, WORLD, tick=1, head=head)
    assert await restore_position(conn, WORLD) == (2, head)


async def test_restore_position_reruns_incomplete_tick(conn):
    """마지막이 started(완주 실패)면 그 tick을 재실행한다 (ADR-011 catch-up)."""
    head = await run_tick(conn, NoopPhases(), CLOCK, WORLD, tick=0, head=0)

    class Crash(Exception): ...

    class CrashingPhases(NoopPhases):
        async def decide(self, ctx):
            raise Crash

    try:
        await run_tick(conn, CrashingPhases(), CLOCK, WORLD, tick=1, head=head)
    except Crash:
        pass
    next_tick, _ = await restore_position(conn, WORLD)
    assert next_tick == 1  # completed가 없으니 tick 1을 다시


async def test_loop_completes_ticks_until_stopped(conn):
    cfg = TickConfig(
        pg_dsn=PG_DSN, world_id=WORLD, genesis=GENESIS, real_seconds_per_tick=0.01
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run_tick_loop(cfg, NoopPhases(), stop=stop))

    for _ in range(200):  # 3 tick 완주까지 대기
        events = await read_stream(conn, WORLD, "system", "tick")
        if sum(e.envelope["type"] == "system.tick.completed" for e in events) >= 3:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    events = await read_stream(conn, WORLD, "system", "tick")
    completed = [e.envelope for e in events if e.envelope["type"] == "system.tick.completed"]
    assert len(completed) >= 3
    ticks = [c["payload"]["tick"] for c in completed]
    assert ticks == list(range(len(ticks)))  # 연속 — 건너뛰기 금지 (ADR-011)


async def test_world_leadership_is_exclusive(conn):
    assert await try_acquire_world_leadership(conn, WORLD)
    async with await AsyncConnection.connect(PG_DSN, autocommit=True) as standby:
        assert not await try_acquire_world_leadership(standby, WORLD)
        # 다른 세계는 별도 락
        assert await try_acquire_world_leadership(standby, "w_other")
