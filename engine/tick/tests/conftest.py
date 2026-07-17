import asyncio
import sys

import pytest
from lf_eventstore.migrate import migrate
from lf_eventstore.testing import SKIP_DB, assert_test_database, test_database_url
from psycopg import AsyncConnection

# psycopg async는 Windows ProactorEventLoop에서 동작하지 않는다 (로컬 개발 전용)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 파괴적 픽스처는 명시된 전용 인프라만 겨눈다 — 미설정은 skip, 규약 위반은 실패
# (lf_eventstore.testing, 2026-07-17 사고 2건의 가드)
PG_DSN = test_database_url()


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
