import asyncio
import json
import sys
from pathlib import Path

import pytest
from lf_eventstore.testing import (
    SKIP_DB,
    SKIP_NATS,
    SKIP_REDIS,
    assert_test_database,
    assert_test_nats,
    test_database_url,
    test_nats_url,
    test_redis_url,
)
from psycopg import AsyncConnection
from redis.asyncio import Redis

# psycopg async는 Windows ProactorEventLoop에서 동작하지 않는다 (로컬 개발 전용)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

SAMPLES_DIR = Path(__file__).resolve().parents[3] / "packages" / "schemas" / "samples"

# 파괴적 픽스처는 명시된 전용 인프라만 겨눈다 — 미설정은 skip, 규약 위반은 실패
# (lf_eventstore.testing, 2026-07-17 사고 2건의 가드)
PG_DSN = test_database_url()
REDIS_URL = test_redis_url()
NATS_URL = test_nats_url()


def sample(name: str) -> dict:
    """packages/schemas/samples의 검증된 봉투 — 테스트 재료의 유일 출처."""
    return json.loads((SAMPLES_DIR / f"{name}.001.json").read_text(encoding="utf-8"))


@pytest.fixture
async def pg():
    """read 스키마가 비워진 PG 연결 (lf-actor conftest와 동일 게이트 규약)."""
    if PG_DSN is None:
        pytest.skip(SKIP_DB)
    assert_test_database(PG_DSN)
    conn = await AsyncConnection.connect(PG_DSN, connect_timeout=3, autocommit=True)
    async with conn:
        await conn.execute("DROP SCHEMA IF EXISTS read CASCADE")
        yield conn


@pytest.fixture
async def js():
    """스트림이 초기화된 JetStream 컨텍스트 — 마커 검증된 테스트 서버에서만."""
    import nats
    from lf_dispatcher.streams import STREAMS, ensure_streams
    from nats.js.errors import NotFoundError

    if NATS_URL is None:
        pytest.skip(SKIP_NATS)
    nc = await asyncio.wait_for(nats.connect(NATS_URL, connect_timeout=3), timeout=5)
    try:
        context = nc.jetstream()
        await assert_test_nats(context)  # 상주 세계면 여기서 실패한다
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
