"""Tick Engine — 세계의 심장 박동 (ADR-011).

세계당 1 리더(PG advisory lock). tick이 실시간 예산(60s)을 초과하면
다음 tick을 미루고 완주한다 — 건너뛰기는 역사 공백이므로 금지.
중단 후 재개는 마지막 tick부터 이어간다 (catch-up 정책).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from lf_eventstore import NewEvent, append, current_head, read_stream
from psycopg import AsyncConnection

from lf_tick.clock import TickClock
from lf_tick.config import TickConfig
from lf_tick.pipeline import TickContext, TickPhases

logger = logging.getLogger("lf.tick.engine")

PRINCIPAL = "engine.tick"
TICK_STREAM = "system"
TICK_STREAM_KEY = "tick"


def _isoformat_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


async def try_acquire_world_leadership(conn: AsyncConnection, world_id: str) -> bool:
    """세계당 1 tick engine — 세션 advisory lock (ADR-019 §워크로드 배치)."""
    cur = await conn.execute(
        "SELECT pg_try_advisory_lock(hashtext(%s))", (f"lf_tick:{world_id}",)
    )
    row = await cur.fetchone()
    assert row is not None
    return bool(row[0])


async def restore_position(conn: AsyncConnection, world_id: str) -> tuple[int, int]:
    """(다음 tick 번호, 현재 stream head) — 중단 지점부터 이어간다 (ADR-011).

    마지막 이벤트가 started(완주 실패)면 그 tick을 재실행한다 — started가
    한 번 더 적재되지만, 재시작 사실 자체가 역사다 (소비자는 event_id 멱등).
    """
    head = await current_head(conn, world_id, TICK_STREAM, TICK_STREAM_KEY)
    if head == 0:
        return 0, 0
    [last] = await read_stream(conn, world_id, TICK_STREAM, TICK_STREAM_KEY, from_seq=head)
    last_tick = int(last.envelope["payload"]["tick"])
    if last.envelope["type"] == "system.tick.completed":
        return last_tick + 1, head
    return last_tick, head


async def run_tick(
    conn: AsyncConnection,
    phases: TickPhases,
    clock: TickClock,
    world_id: str,
    tick: int,
    head: int,
) -> int:
    """단일 tick 파이프라인을 완주한다. 반환: 새 stream head."""
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    ctx = TickContext(
        world_id=world_id, tick=tick, world_time=clock.world_time_at(tick), conn=conn
    )

    scheduled = await phases.schedule(ctx)
    [started] = await append(
        conn,
        PRINCIPAL,
        [
            NewEvent(
                world_id=world_id,
                stream=TICK_STREAM,
                stream_key=TICK_STREAM_KEY,
                type="system.tick.started",
                tick=tick,
                payload={
                    "tick": tick,
                    "started_at": _isoformat_z(started_at),
                    "scheduled": scheduled,
                },
            )
        ],
        expected_head=head,
    )
    head += 1

    await phases.world(ctx)
    await phases.perceive(ctx)
    decided = await phases.decide(ctx)
    emitted = await phases.resolve(ctx)
    await phases.consolidate(ctx)

    completed_at = datetime.now(UTC)
    await append(
        conn,
        PRINCIPAL,
        [
            NewEvent(
                world_id=world_id,
                stream=TICK_STREAM,
                stream_key=TICK_STREAM_KEY,
                type="system.tick.completed",
                tick=tick,
                causation_id=started.envelope["event_id"],
                correlation_id=started.envelope["correlation_id"],
                payload={
                    "tick": tick,
                    "started_at": _isoformat_z(started_at),
                    "completed_at": _isoformat_z(completed_at),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "actors_decided": decided,
                    "events_emitted": emitted,
                },
            )
        ],
        expected_head=head,
    )
    return head + 1


async def run_tick_loop(
    cfg: TickConfig, phases: TickPhases, *, stop: asyncio.Event | None = None
) -> None:
    """tick 메인 루프 — 리더 획득 후 real_seconds_per_tick 주기로 tick을 구동한다."""
    from lf_eventstore.migrate import migrate

    stop = stop or asyncio.Event()
    clock = TickClock(
        genesis=cfg.genesis, world_seconds_per_tick=cfg.world_seconds_per_tick
    )

    async with await AsyncConnection.connect(cfg.pg_dsn, autocommit=True) as conn:
        for name in await migrate(conn):
            logger.info("마이그레이션 적용: %s", name)

        while not await try_acquire_world_leadership(conn, cfg.world_id):
            logger.info("standby — %s 의 활성 tick engine이 있다", cfg.world_id)
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.standby_retry_s)
                return
            except TimeoutError:
                continue

        tick, head = await restore_position(conn, cfg.world_id)
        logger.info(
            "tick engine 리더 — world=%s tick=%d부터 (세계시간 %s)",
            cfg.world_id, tick, clock.world_time_at(tick).isoformat(),
        )

        loop = asyncio.get_running_loop()
        while not stop.is_set():
            started = loop.time()
            head = await run_tick(conn, phases, clock, cfg.world_id, tick, head)
            elapsed = loop.time() - started
            if elapsed > cfg.real_seconds_per_tick:
                # 미루기 정책: 세계시간이 잠시 느려진다 — 건너뛰기 금지 (ADR-011)
                logger.warning(
                    "tick %d 이 예산 초과 (%.1fs > %.0fs) — 다음 tick을 미룬다",
                    tick, elapsed, cfg.real_seconds_per_tick,
                )
            tick += 1

            delay = max(0.0, cfg.real_seconds_per_tick - elapsed)
            if delay:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
