"""마이그레이션 멱등성 + ULID 생성 검증."""

import re

from lf_eventstore.migrate import migrate, migration_files
from lf_eventstore.ulid import new_ulid

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


async def test_migrate_is_idempotent(conn):
    # conftest가 이미 한 번 적용했다 — 재실행은 no-op이어야 한다
    assert await migrate(conn) == []


def test_migration_files_sorted():
    names = [name for name, _ in migration_files()]
    assert names == sorted(names)
    assert names[0] == "0001_event_store.sql"


def test_migration_files_include_correlation_chain_index():
    names = [name for name, _ in migration_files()]
    assert "0002_events_by_world_correlation.sql" in names


async def test_correlation_chain_index_exists(conn):
    """(world_id, correlation_id) 인덱스 — 서사 사슬 조회(feed-api /story)의 전제."""
    cur = await conn.execute(
        """
        SELECT indexdef FROM pg_indexes
        WHERE schemaname = 'es' AND indexname = 'events_by_world_correlation'
        """
    )
    row = await cur.fetchone()
    assert row is not None
    assert "world_id, correlation_id" in row[0]


def test_ulid_format():
    for _ in range(200):
        assert ULID_RE.match(new_ulid())


def test_ulid_timestamp_ordering():
    earlier = new_ulid(timestamp_ms=1_000_000)
    later = new_ulid(timestamp_ms=2_000_000)
    assert earlier < later  # Crockford base32는 사전순 = 시간순


def test_ulid_uniqueness():
    batch = {new_ulid() for _ in range(10_000)}
    assert len(batch) == 10_000
