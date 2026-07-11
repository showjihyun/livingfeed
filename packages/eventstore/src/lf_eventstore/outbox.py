"""outbox 소비 헬퍼 — dispatcher relay가 사용한다 (ADR-005 §Transactional Outbox, ADR-017 §1).

발행 순서는 global_seq 오름차순이다. 동시 append 트랜잭션의 커밋 순서가
global_seq 순서와 어긋나는 짧은 창(ms 단위)이 존재할 수 있으나,
스트림 내 순서는 stream_heads 직렬화로 항상 보존되고 소비자는 멱등이므로
전역 순서의 미세한 흔들림은 Phase 1에서 허용한다 (ADR-005 §전달 보장).
"""

from __future__ import annotations

from datetime import timedelta

from psycopg import AsyncConnection

from lf_eventstore.model import OutboxRow


async def fetch_unpublished(conn: AsyncConnection, *, limit: int = 500) -> list[OutboxRow]:
    """미발행 행을 global_seq 순으로 가져온다."""
    cur = await conn.execute(
        """
        SELECT global_seq, event_id, envelope, enqueued_at
        FROM es.outbox WHERE published_at IS NULL
        ORDER BY global_seq LIMIT %s
        """,
        (limit,),
    )
    return [
        OutboxRow(global_seq=row[0], event_id=row[1], envelope=row[2], enqueued_at=row[3])
        async for row in cur
    ]


async def mark_published(conn: AsyncConnection, global_seqs: list[int]) -> int:
    """JetStream publish ack 수신 후 발행 완료로 마킹한다. 반환: 마킹된 행 수."""
    if not global_seqs:
        return 0
    cur = await conn.execute(
        "UPDATE es.outbox SET published_at = now()"
        " WHERE global_seq = ANY(%s) AND published_at IS NULL",
        (global_seqs,),
    )
    return cur.rowcount


async def purge_published(conn: AsyncConnection, *, keep: timedelta) -> int:
    """발행 완료 후 keep 이상 지난 행을 정리한다. 반환: 삭제된 행 수."""
    cur = await conn.execute(
        "DELETE FROM es.outbox WHERE published_at IS NOT NULL AND published_at < now() - %s",
        (keep,),
    )
    return cur.rowcount


async def outbox_lag(conn: AsyncConnection) -> int:
    """미발행 행 수 — outbox_lag 메트릭의 원천 (ADR-005/020)."""
    cur = await conn.execute("SELECT count(*) FROM es.outbox WHERE published_at IS NULL")
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])
