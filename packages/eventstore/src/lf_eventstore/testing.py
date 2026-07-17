"""파괴적 테스트 픽스처의 공용 가드 — 상주 인프라 보호 (2026-07-17 사고 2건의 교훈).

두 원칙:

1. **명시가 없으면 겨누지 않는다.** LF_TEST_* env가 없으면 파괴적 픽스처는
   skip이다 — 기본값으로 로컬 4222/5432를 겨누던 것이 사고의 형태였다
   (상주 세계의 es 스키마 드롭, LF_* 스트림 삭제).
2. **명시돼 있어도 표적을 검증한다.** env가 실수로 상주 인프라를 가리켜도
   가드가 막는다: PG는 DB 이름 `_test` 접미 강제, NATS는 마커 스트림
   (LF_* 스트림이 하나도 없는 서버에만 자동 생성)이 있는 서버만 허용.
   규약 위반은 skip이 아니라 **실패**다 — 조용히 넘어가면 다음 사람이 밟는다.

사용 (conftest):

    from lf_eventstore.testing import (
        SKIP_DB, SKIP_NATS, SKIP_REDIS,
        assert_test_database, assert_test_nats,
        test_database_url, test_nats_url, test_redis_url,
    )

    PG_DSN = test_database_url()

    @pytest.fixture
    async def conn():
        if PG_DSN is None:
            pytest.skip(SKIP_DB)
        assert_test_database(PG_DSN)
        ...
"""

from __future__ import annotations

import os
from typing import Any

from psycopg import conninfo

#: 테스트 NATS의 신원 표식 — LF_* 스트림이 전무한 서버에서 최초 1회 자동 생성된다.
#: 상주 세계의 서버는 LF_* 스트림을 갖고 마커가 없으므로 무엇을 겨눠도 거부된다.
TEST_MARKER_STREAM = "LF_TEST_MARKER"

SKIP_DB = (
    "LF_TEST_DATABASE_URL 미설정 — 파괴적 픽스처(es/read 스키마 드롭)는 전용 "
    "테스트 DB(…5433/livingfeed_test)가 명시될 때만 돈다 (2026-07-17 사고 교훈)"
)
SKIP_NATS = (
    "LF_TEST_NATS_URL 미설정 — 파괴적 픽스처(LF_* 스트림 삭제)는 전용 테스트 "
    "NATS(compose nats-test, localhost:4223)가 명시될 때만 돈다 (2026-07-17 사고 교훈)"
)
SKIP_REDIS = (
    "LF_TEST_REDIS_URL 미설정 — flushdb 픽스처는 전용 테스트 DB"
    "(redis://localhost:6380/15)가 명시될 때만 돈다"
)


def test_database_url() -> str | None:
    return os.environ.get("LF_TEST_DATABASE_URL") or None


def test_nats_url() -> str | None:
    return os.environ.get("LF_TEST_NATS_URL") or None


def test_redis_url() -> str | None:
    return os.environ.get("LF_TEST_REDIS_URL") or None


def assert_test_database(dsn: str) -> None:
    """DB 이름이 `_test` 접미가 아니면 실패 — 상주 DB에는 스키마 드롭을 못 겨눈다."""
    dbname = str(conninfo.conninfo_to_dict(dsn).get("dbname") or "")
    if not dbname.endswith("_test"):
        raise RuntimeError(
            f"LF_TEST_DATABASE_URL의 DB 이름이 '_test' 접미가 아니다: {dbname!r} — "
            "파괴적 픽스처는 전용 테스트 DB에만 허용된다. "
            '없으면: docker exec livingfeed-postgres-1 psql -U livingfeed -d postgres '
            '-c "CREATE DATABASE livingfeed_test;" (2026-07-17 상주 세계 파괴 사고의 가드)'
        )


async def assert_test_nats(js: Any) -> None:
    """마커 스트림이 있는 서버만 파괴를 허용한다.

    마커가 없으면: LF_* 스트림이 하나도 없는(=갓 뜬 테스트 전용) 서버에만
    마커를 만들고 통과시킨다. LF_* 스트림을 가진 무마커 서버는 상주 세계로
    보고 실패한다 — CI의 일회용 서버는 첫 실행에서 마커를 얻는다.
    """
    from nats.js.errors import NotFoundError

    try:
        await js.stream_info(TEST_MARKER_STREAM)
        return
    except NotFoundError:
        pass
    try:
        existing = [s.config.name for s in await js.streams_info()]
    except NotFoundError:  # 스트림이 하나도 없는 서버는 404를 주기도 한다
        existing = []
    lf_streams = [name for name in existing if name and name.startswith("LF_")]
    if lf_streams:
        raise RuntimeError(
            f"이 NATS에는 LF_* 스트림({', '.join(sorted(lf_streams)[:3])}…)이 있고 "
            "테스트 마커가 없다 — 상주 세계로 보인다. 파괴적 픽스처를 중단한다. "
            "전용 테스트 NATS(compose nats-test, localhost:4223)를 겨눠라 "
            "(2026-07-17 스트림 전소 사고의 가드)"
        )
    await js.add_stream(name=TEST_MARKER_STREAM, subjects=["lf-test.marker"])
