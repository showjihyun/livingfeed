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
from typing import Any

from lf_eventstore import NewEvent, Provenance, append, current_head, read_stream
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


async def _run_phases(phases: TickPhases, ctx: TickContext) -> tuple[dict, int]:
    """tick 파이프라인의 액터 구간 — 리더의 자기 샤드와 팔로워가 공유하는 몸통."""
    await phases.world(ctx)
    await phases.perceive(ctx)
    decided = await phases.decide(ctx)
    emitted = await phases.resolve(ctx)
    await phases.consolidate(ctx)
    return decided, emitted


def _merge_counts(decided: dict, emitted: int, acks: dict[int, dict]) -> tuple[dict, int]:
    """샤드 ack들을 리더 몫에 합산한다 — completed 계수는 세계 전체다."""
    total = dict(decided)
    for report in acks.values():
        for tier, n in report.get("decided", {}).items():
            total[tier] = total.get(tier, 0) + int(n)
        emitted += int(report.get("emitted", 0))
    return total, emitted


async def run_tick(
    conn: AsyncConnection,
    phases: TickPhases,
    clock: TickClock,
    world_id: str,
    tick: int,
    head: int,
    *,
    barrier: Any | None = None,
    ack_timeout_s: float = 600.0,
) -> int:
    """단일 tick 파이프라인을 완주한다. 반환: 새 stream head.

    barrier(ShardBarrier)가 있으면 분산 모드다 (ADR-012 Phase 2): 리더는
    started 적재 후 타 샤드에 신호를 보내고, 자기 샤드를 실행한 뒤 ack를
    집계해 completed에 세계 전체 계수를 싣는다. 없으면(솔로) 오늘과 동일.
    """
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
                # 세계 시계의 기계적 사실 — 해석이 개입하지 않는다 (ADR-021 §1)
                provenance=Provenance.derived("tick.pipeline:started"),
                payload={
                    "tick": tick,
                    "started_at": _isoformat_z(started_at),
                    # 분산 모드의 scheduled는 리더 샤드 몫만이다 — 세계 전체
                    # 계수는 completed.actors_decided가 권위다
                    "scheduled": scheduled,
                },
            )
        ],
        expected_head=head,
    )
    head += 1

    if barrier is not None and barrier.others:
        await barrier.signal(tick)  # 타 샤드 출발 — 리더 샤드와 병렬로 달린다
    decided, emitted = await _run_phases(phases, ctx)
    if barrier is not None and barrier.others:
        acks = await barrier.wait_acks(tick, timeout_s=ack_timeout_s)
        decided, emitted = _merge_counts(decided, emitted, acks)

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
                provenance=Provenance.derived("tick.pipeline:completed"),
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


async def _lead(
    conn: AsyncConnection,
    cfg: TickConfig,
    phases: TickPhases,
    clock: TickClock,
    stop: asyncio.Event,
    barrier: Any | None,
) -> None:
    """리더 본체 — 리더십을 쥔 채 tick을 구동한다."""
    tick, head = await restore_position(conn, cfg.world_id)
    distributed = barrier is not None and barrier.others
    logger.info(
        "tick engine 리더 — world=%s tick=%d부터 (세계시간 %s)%s",
        cfg.world_id, tick, clock.world_time_at(tick).isoformat(),
        f" [분산: 타 샤드 {sorted(barrier.others)}]" if distributed else "",
    )

    loop = asyncio.get_running_loop()
    while not stop.is_set():
        started = loop.time()
        head = await run_tick(
            conn, phases, clock, cfg.world_id, tick, head, barrier=barrier
        )
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


async def run_tick_loop(
    cfg: TickConfig,
    phases: TickPhases,
    *,
    stop: asyncio.Event | None = None,
    barrier: Any | None = None,
) -> None:
    """tick 메인 루프 — 리더 획득 후 real_seconds_per_tick 주기로 tick을 구동한다.

    barrier가 있으면(분산 모드, ADR-012 Phase 2) 리더 실패 시 standby가 아니라
    **팔로워**가 된다: 리더의 신호를 받아 자기 샤드의 phases를 돌리고 ack한다.
    신호가 끊기면(리더 사망) 리더십을 재시도한다 — 페일오버는 신호 침묵이 알린다.
    """
    from lf_eventstore.migrate import migrate

    stop = stop or asyncio.Event()
    clock = TickClock(
        genesis=cfg.genesis, world_seconds_per_tick=cfg.world_seconds_per_tick
    )

    async with await AsyncConnection.connect(cfg.pg_dsn, autocommit=True) as conn:
        for name in await migrate(conn):
            logger.info("마이그레이션 적용: %s", name)

        while not stop.is_set():
            if await try_acquire_world_leadership(conn, cfg.world_id):
                await _lead(conn, cfg, phases, clock, stop, barrier)
                return

            if barrier is None:
                # 솔로 배선의 standby — 기존 동작 그대로 (재시도 후 리더 승계)
                logger.info("standby — %s 의 활성 tick engine이 있다", cfg.world_id)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=cfg.standby_retry_s)
                    return
                except TimeoutError:
                    continue

            # 팔로워 — 신호가 침묵하면(리더 사망 의심) 루프 머리로 돌아가
            # 리더십을 재시도한다 (자기 샤드는 그 사이 침묵, 미루기 정신)
            logger.info(
                "샤드 팔로워 — world=%s own 대기 (리더 신호 기반)", cfg.world_id
            )
            silence_budget = cfg.real_seconds_per_tick * 4
            while not stop.is_set():
                tick = await barrier.next_signal(timeout_s=silence_budget)
                if tick is None:
                    logger.info("리더 신호 침묵 %.0fs — 리더십 재시도", silence_budget)
                    break
                ctx = TickContext(
                    world_id=cfg.world_id, tick=tick,
                    world_time=clock.world_time_at(tick), conn=conn,
                )
                # schedule은 팔로워에게도 tick 경계 훅이다 (핫 리로드 등) —
                # 계수는 리더 것이 아니므로 버린다
                await phases.schedule(ctx)
                decided, emitted = await _run_phases(phases, ctx)
                await barrier.ack(tick, {"decided": decided, "emitted": emitted})
