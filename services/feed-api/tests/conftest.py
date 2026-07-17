import asyncio
import json
import sys
from pathlib import Path

import pytest
from lf_eventstore.testing import SKIP_DB, assert_test_database, test_database_url
from psycopg import AsyncConnection

# psycopg async는 Windows ProactorEventLoop에서 동작하지 않는다 (로컬 개발 전용)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

SAMPLES_DIR = Path(__file__).resolve().parents[3] / "packages" / "schemas" / "samples"

# 파괴적 픽스처는 명시된 전용 인프라만 겨눈다 — 미설정은 skip, 규약 위반은 실패
# (lf_eventstore.testing, 2026-07-17 사고 2건의 가드)
PG_DSN = test_database_url()


def sample(name: str) -> dict:
    """packages/schemas/samples의 검증된 봉투 — 테스트 재료의 유일 출처."""
    return json.loads((SAMPLES_DIR / f"{name}.001.json").read_text(encoding="utf-8"))


@pytest.fixture
async def pg():
    """read 스키마가 비워진 PG 연결 (lf-projector conftest와 동일 게이트 규약)."""
    if PG_DSN is None:
        pytest.skip(SKIP_DB)
    assert_test_database(PG_DSN)
    conn = await AsyncConnection.connect(PG_DSN, connect_timeout=3, autocommit=True)
    async with conn:
        await conn.execute("DROP SCHEMA IF EXISTS read CASCADE")
        yield conn
