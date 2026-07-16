import asyncio
import json
import os
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg import AsyncConnection
from redis.asyncio import Redis

# psycopg async는 Windows ProactorEventLoop에서 동작하지 않는다 (로컬 개발 전용)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

SAMPLES_DIR = Path(__file__).resolve().parents[3] / "packages" / "schemas" / "samples"

PG_DSN = os.environ.get(
    "LF_TEST_DATABASE_URL",
    "postgresql://livingfeed:livingfeed@localhost:5432/livingfeed",
)
REDIS_URL = os.environ.get("LF_TEST_REDIS_URL", "redis://localhost:6379/15")
NATS_URL = os.environ.get("LF_TEST_NATS_URL", "nats://localhost:4222")


def sample(name: str) -> dict:
    """packages/schemas/samples의 검증된 봉투 — 테스트 재료의 유일 출처."""
    return json.loads((SAMPLES_DIR / f"{name}.001.json").read_text(encoding="utf-8"))


@pytest.fixture
async def pg():
    """read 스키마가 비워진 PG 연결 (lf-actor conftest와 동일 게이트 규약)."""
    try:
        conn = await AsyncConnection.connect(PG_DSN, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError:
        if "LF_TEST_DATABASE_URL" in os.environ:
            raise
        pytest.skip(f"PostgreSQL 미가용 ({PG_DSN}) — infra/compose에서 postgres를 켜라")
    async with conn:
        await conn.execute("DROP SCHEMA IF EXISTS read CASCADE")
        yield conn


@pytest.fixture
async def js():
    """스트림이 초기화된 JetStream 컨텍스트 (dispatcher conftest와 동일 규약)."""
    import nats
    from lf_dispatcher.streams import STREAMS, ensure_streams
    from nats.js.errors import NotFoundError

    try:
        nc = await asyncio.wait_for(nats.connect(NATS_URL, connect_timeout=3), timeout=5)
    except Exception:
        if "LF_TEST_NATS_URL" in os.environ:
            raise
        pytest.skip(f"NATS 미가용 ({NATS_URL}) — infra/compose에서 nats를 켜라")
    try:
        context = nc.jetstream()
        for spec in STREAMS:  # 테스트 격리: 이전 실행의 메시지 제거
            try:
                await context.delete_stream(spec.name)
            except NotFoundError:
                pass
        await ensure_streams(context)
        yield context
    finally:
        await nc.close()


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
