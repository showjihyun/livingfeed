import asyncio
import sys
from pathlib import Path

import pytest
from lf_eventstore.migrate import migrate
from lf_eventstore.testing import (
    SKIP_DB,
    SKIP_REDIS,
    assert_test_database,
    test_database_url,
    test_nats_url,
    test_redis_url,
)
from psycopg import AsyncConnection
from redis.asyncio import Redis

# psycopg async는 Windows ProactorEventLoop에서 동작하지 않는다 (로컬 개발 전용)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

REPO_ROOT = Path(__file__).resolve().parents[3]
PERSONAS_DIR = REPO_ROOT / "agents" / "personas"

# 파괴적 픽스처는 명시된 전용 인프라만 겨눈다 — 미설정은 skip, 규약 위반은 실패
# (lf_eventstore.testing, 2026-07-17 사고 2건의 가드)
PG_DSN = test_database_url()
REDIS_URL = test_redis_url()
NATS_URL = test_nats_url()


@pytest.fixture
async def conn():
    """마이그레이션 적용된 깨끗한 es 스키마 연결 (lf-eventstore conftest와 동일 규약)."""
    if PG_DSN is None:
        pytest.skip(SKIP_DB)
    assert_test_database(PG_DSN)
    connection = await AsyncConnection.connect(PG_DSN, connect_timeout=3, autocommit=True)
    async with connection:
        await connection.execute("DROP SCHEMA IF EXISTS es CASCADE")
        await migrate(connection)
        yield connection


@pytest.fixture
async def redis():
    """깨끗한 테스트 전용 DB의 Redis 연결 — flushdb는 명시된 표적에만."""
    if REDIS_URL is None:
        pytest.skip(SKIP_REDIS)
    client = Redis.from_url(REDIS_URL)
    await client.ping()
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()
