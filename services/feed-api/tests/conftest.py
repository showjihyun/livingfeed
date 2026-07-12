import asyncio
import json
import os
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg import AsyncConnection

# psycopg async는 Windows ProactorEventLoop에서 동작하지 않는다 (로컬 개발 전용)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

SAMPLES_DIR = Path(__file__).resolve().parents[3] / "packages" / "schemas" / "samples"

PG_DSN = os.environ.get(
    "LF_TEST_DATABASE_URL",
    "postgresql://livingfeed:livingfeed@localhost:5432/livingfeed",
)


def sample(name: str) -> dict:
    """packages/schemas/samples의 검증된 봉투 — 테스트 재료의 유일 출처."""
    return json.loads((SAMPLES_DIR / f"{name}.001.json").read_text(encoding="utf-8"))


@pytest.fixture
async def pg():
    """read 스키마가 비워진 PG 연결 (lf-projector conftest와 동일 게이트 규약)."""
    try:
        conn = await AsyncConnection.connect(PG_DSN, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError:
        if "LF_TEST_DATABASE_URL" in os.environ:
            raise
        pytest.skip(f"PostgreSQL 미가용 ({PG_DSN}) — infra/compose에서 postgres를 켜라")
    async with conn:
        await conn.execute("DROP SCHEMA IF EXISTS read CASCADE")
        yield conn
