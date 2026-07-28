"""relay 통합 검증 — 실제 PostgreSQL + NATS JetStream 대상 (conftest 참고)."""

import json

import nats.errors
from lf_dispatcher.relay import RELAY_LOCK_KEY, relay_once, try_acquire_leadership
from lf_dispatcher.streams import ensure_streams
from lf_eventstore import NewEvent, Provenance, append, outbox_lag
from psycopg import AsyncConnection

from .conftest import PG_DSN

ENV = "test"

TICK_PAYLOAD = {
    "tick": 1,
    "started_at": "2026-07-12T00:00:00Z",
    "completed_at": "2026-07-12T00:00:41Z",
    "duration_ms": 41_000,
    "actors_decided": {"hot": 1, "warm": 2, "cold": 3},
    "events_emitted": 4,
}


def tick_event(n: int) -> NewEvent:
    return NewEvent(
        world_id="w_test",
        stream="system",
        stream_key="tick",
        type="system.tick.completed",
        tick=n,
        payload={**TICK_PAYLOAD, "tick": n},
        provenance=Provenance.derived("tick.pipeline:completed"),
    )


async def drain(js, stream: str, batch: int = 10) -> list:
    sub = await js.pull_subscribe("lf.>", stream=stream)
    try:
        return await sub.fetch(batch, timeout=2)
    except (nats.errors.TimeoutError, TimeoutError):
        return []
    finally:
        await sub.unsubscribe()


async def test_relay_publishes_in_order(conn, js):
    for i in range(3):
        await append(conn, "engine.tick", [tick_event(i)], expected_head=i)

    published = await relay_once(conn, js, ENV)
    assert published == 3
    assert await outbox_lag(conn) == 0

    msgs = await drain(js, "LF_SYS")
    assert len(msgs) == 3
    assert all(m.subject == f"lf.{ENV}.w_test.system.tick.completed" for m in msgs)
    ticks = [json.loads(m.data)["payload"]["tick"] for m in msgs]
    assert ticks == [0, 1, 2]  # global_seq 순 발행 (ADR-017 §1)


async def test_relay_noop_when_empty(conn, js):
    assert await relay_once(conn, js, ENV) == 0


async def test_republish_is_deduplicated(conn, js):
    await append(conn, "engine.tick", [tick_event(0)], expected_head=0)
    assert await relay_once(conn, js, ENV) == 1

    # 장애 시나리오 재현: 발행됐지만 마킹이 유실됨 → 재발행 (at-least-once)
    await conn.execute("UPDATE es.outbox SET published_at = NULL")
    assert await relay_once(conn, js, ENV) == 1

    # Nats-Msg-Id dedup window가 중복을 흡수한다 (ADR-017 §1)
    info = await js.stream_info("LF_SYS")
    assert info.state.messages == 1


async def test_invalid_envelope_goes_to_dlq(conn, js):
    # 적재 게이트를 우회한 오염 행(구버전 잔재 시나리오)을 직접 만든다
    bogus = {"event_id": "not-a-ulid", "type": "mystery"}
    await conn.execute(
        "INSERT INTO es.outbox (global_seq, event_id, envelope) VALUES (1, %s, %s::jsonb)",
        ("not-a-ulid", json.dumps(bogus)),
    )

    assert await relay_once(conn, js, ENV) == 1
    assert await outbox_lag(conn) == 0  # DLQ로 이동했어도 outbox에 남기지 않는다

    dlq_info = await js.stream_info("LF_DLQ")
    assert dlq_info.state.messages == 1
    sys_info = await js.stream_info("LF_SYS")
    assert sys_info.state.messages == 0


async def test_leader_election_is_exclusive(conn):
    assert await try_acquire_leadership(conn)
    async with await AsyncConnection.connect(PG_DSN, autocommit=True) as standby:
        assert not await try_acquire_leadership(standby)
    await conn.execute("SELECT pg_advisory_unlock(%s)", (RELAY_LOCK_KEY,))


async def test_ensure_streams_is_idempotent(js):
    await ensure_streams(js)  # conftest에서 이미 한 번 — 재실행에도 예외가 없어야 한다
    info = await js.stream_info("LF_ACTOR")
    assert "lf.*.*.actor.>" in info.config.subjects
