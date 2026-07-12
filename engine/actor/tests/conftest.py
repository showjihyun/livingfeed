import asyncio
import os
import sys
from pathlib import Path

import psycopg
import pytest
from lf_eventstore.migrate import migrate
from psycopg import AsyncConnection
from redis.asyncio import Redis

# psycopg async는 Windows ProactorEventLoop에서 동작하지 않는다 (로컬 개발 전용)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

REPO_ROOT = Path(__file__).resolve().parents[3]
PERSONAS_DIR = REPO_ROOT / "agents" / "personas"

PG_DSN = os.environ.get(
    "LF_TEST_DATABASE_URL",
    "postgresql://livingfeed:livingfeed@localhost:5432/livingfeed",
)
REDIS_URL = os.environ.get("LF_TEST_REDIS_URL", "redis://localhost:6379/15")
NATS_URL = os.environ.get("LF_TEST_NATS_URL", "nats://localhost:4222")


@pytest.fixture
async def conn():
    """마이그레이션 적용된 깨끗한 es 스키마 연결 (lf-eventstore conftest와 동일 규약)."""
    try:
        connection = await AsyncConnection.connect(PG_DSN, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError:
        if "LF_TEST_DATABASE_URL" in os.environ:
            raise
        pytest.skip(f"PostgreSQL 미가용 ({PG_DSN}) — infra/compose에서 postgres를 켜라")
    async with connection:
        await connection.execute("DROP SCHEMA IF EXISTS es CASCADE")
        await migrate(connection)
        yield connection


@pytest.fixture
async def redis():
    """깨끗한 테스트 전용 DB(기본 15)의 Redis 연결."""
    client = Redis.from_url(REDIS_URL)
    try:
        await client.ping()
    except Exception:
        if "LF_TEST_REDIS_URL" in os.environ:
            raise
        await client.aclose()
        pytest.skip(f"Redis 미가용 ({REDIS_URL}) — infra/compose에서 redis를 켜라")
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()
