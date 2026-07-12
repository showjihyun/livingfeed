import asyncio
import os
import sys

import psycopg
import pytest
from lf_eventstore.migrate import migrate
from psycopg import AsyncConnection

# psycopg async는 Windows ProactorEventLoop에서 동작하지 않는다 (로컬 개발 전용)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

PG_DSN = os.environ.get(
    "LF_TEST_DATABASE_URL",
    "postgresql://livingfeed:livingfeed@localhost:5432/livingfeed",
)
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
async def js():
    """스트림이 초기화된 JetStream 컨텍스트. NATS 미가용이면 skip (CI는 fail)."""
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
