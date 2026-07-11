import asyncio
import os
import sys

import psycopg
import pytest
from lf_eventstore.migrate import migrate
from psycopg import AsyncConnection

# psycopg async는 Windows ProactorEventLoop에서 동작하지 않는다 (로컬 개발 전용 —
# 프로덕션은 Linux 컨테이너). pytest-asyncio가 만드는 루프에 Selector 정책을 강제한다.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

DSN = os.environ.get(
    "LF_TEST_DATABASE_URL",
    "postgresql://livingfeed:livingfeed@localhost:5432/livingfeed",
)


@pytest.fixture
async def conn():
    """마이그레이션이 적용된 깨끗한 es 스키마를 가진 연결.

    PostgreSQL이 없으면 skip — 로컬은 compose core 프로파일, CI는 서비스 컨테이너.
    autocommit=True: append가 자체 트랜잭션을 열기 때문 (README 참고).
    """
    try:
        connection = await AsyncConnection.connect(DSN, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError:
        if "LF_TEST_DATABASE_URL" in os.environ:
            raise  # 명시적으로 지정된 DB(CI 등)에 접속 불가 — 환경 오류이므로 fail
        pytest.skip(f"PostgreSQL 미가용 ({DSN}) — infra/compose에서 postgres를 켜라")
    async with connection:
        await connection.execute("DROP SCHEMA IF EXISTS es CASCADE")
        await migrate(connection)
        yield connection
