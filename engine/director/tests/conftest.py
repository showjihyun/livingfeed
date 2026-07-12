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

DSN = os.environ.get(
    "LF_TEST_DATABASE_URL",
    "postgresql://livingfeed:livingfeed@localhost:5432/livingfeed",
)


@pytest.fixture
async def conn():
    """마이그레이션 적용된 깨끗한 es 스키마 연결 (lf-eventstore conftest와 동일 규약)."""
    try:
        connection = await AsyncConnection.connect(DSN, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError:
        if "LF_TEST_DATABASE_URL" in os.environ:
            raise
        pytest.skip(f"PostgreSQL 미가용 ({DSN}) — infra/compose에서 postgres를 켜라")
    async with connection:
        await connection.execute("DROP SCHEMA IF EXISTS es CASCADE")
        await migrate(connection)
        yield connection
