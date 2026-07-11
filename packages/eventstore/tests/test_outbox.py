"""outbox 소비 헬퍼 검증 — relay 계약 (ADR-005/017)."""

from datetime import timedelta

from lf_eventstore import (
    NewEvent,
    append,
    fetch_unpublished,
    mark_published,
    outbox_lag,
    purge_published,
)

TICK_PAYLOAD = {
    "tick": 7,
    "started_at": "2026-07-11T00:00:00Z",
    "completed_at": "2026-07-11T00:00:41Z",
    "duration_ms": 41_000,
    "actors_decided": {"hot": 1, "warm": 2, "cold": 3},
    "events_emitted": 6,
}


def tick_event(n: int) -> NewEvent:
    return NewEvent(
        world_id="w_test",
        stream="system",
        stream_key="tick",
        type="system.tick.completed",
        tick=n,
        payload={**TICK_PAYLOAD, "tick": n},
    )


async def seed(conn, count: int) -> list[int]:
    seqs: list[int] = []
    for i in range(count):
        [stored] = await append(conn, "engine.tick", [tick_event(i)], expected_head=i)
        seqs.append(stored.global_seq)
    return seqs


async def test_fetch_in_global_seq_order(conn):
    seqs = await seed(conn, 3)
    rows = await fetch_unpublished(conn)
    assert [r.global_seq for r in rows] == seqs
    assert rows[0].envelope["type"] == "system.tick.completed"


async def test_mark_published_excludes_from_fetch(conn):
    seqs = await seed(conn, 3)
    assert await mark_published(conn, seqs[:2]) == 2
    remaining = await fetch_unpublished(conn)
    assert [r.global_seq for r in remaining] == seqs[2:]
    assert await outbox_lag(conn) == 1
    # 중복 마킹은 no-op (멱등)
    assert await mark_published(conn, seqs[:2]) == 0


async def test_purge_only_removes_published(conn):
    seqs = await seed(conn, 3)
    await mark_published(conn, seqs[:1])
    deleted = await purge_published(conn, keep=timedelta(0))
    assert deleted == 1
    cur = await conn.execute("SELECT count(*) FROM es.outbox")
    assert (await cur.fetchone())[0] == 2


async def test_fetch_limit(conn):
    await seed(conn, 3)
    rows = await fetch_unpublished(conn, limit=2)
    assert len(rows) == 2
