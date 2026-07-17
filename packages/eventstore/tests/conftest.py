import asyncio
import sys

import pytest
from lf_eventstore.migrate import migrate
from lf_eventstore.testing import SKIP_DB, assert_test_database, test_database_url
from psycopg import AsyncConnection

# psycopg async는 Windows ProactorEventLoop에서 동작하지 않는다 (로컬 개발 전용 —
# 프로덕션은 Linux 컨테이너). pytest-asyncio가 만드는 루프에 Selector 정책을 강제한다.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 파괴적 픽스처는 명시된 전용 인프라만 겨눈다 — 미설정은 skip, 규약 위반은 실패
# (lf_eventstore.testing, 2026-07-17 사고 2건의 가드)
DSN = test_database_url()


@pytest.fixture
async def conn():
    """마이그레이션이 적용된 깨끗한 es 스키마를 가진 연결.

    LF_TEST_DATABASE_URL(_test 접미 DB)이 명시될 때만 돈다 — 로컬은 compose core
    프로파일 + livingfeed_test, CI는 서비스 컨테이너. autocommit=True: append가
    자체 트랜잭션을 열기 때문 (README 참고).
    """
    if DSN is None:
        pytest.skip(SKIP_DB)
    assert_test_database(DSN)
    connection = await AsyncConnection.connect(DSN, connect_timeout=3, autocommit=True)
    async with connection:
        await connection.execute("DROP SCHEMA IF EXISTS es CASCADE")
        await migrate(connection)
        yield connection
