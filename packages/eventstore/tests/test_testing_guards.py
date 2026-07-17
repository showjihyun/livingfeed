"""파괴적 픽스처 가드 검증 — 상주 인프라 보호 (2026-07-17 사고 2건의 회귀 고정)."""

import asyncio

import pytest
from lf_eventstore.testing import (
    TEST_MARKER_STREAM,
    assert_test_database,
    assert_test_nats,
    test_nats_url,
)


def test_dsn_guard_requires_test_suffix():
    # 전용 테스트 DB만 통과 — 상주 DB 이름은 무엇이 붙어 있어도 실패
    assert_test_database("postgresql://u:p@localhost:5433/livingfeed_test")
    with pytest.raises(RuntimeError, match="_test"):
        assert_test_database("postgresql://u:p@localhost:5433/livingfeed")
    with pytest.raises(RuntimeError, match="_test"):
        assert_test_database("postgresql://u:p@localhost:5432/postgres")


def test_dsn_guard_rejects_missing_dbname():
    with pytest.raises(RuntimeError, match="_test"):
        assert_test_database("postgresql://u:p@localhost:5433")


async def _nats_guard_roundtrip(url: str) -> None:
    import nats

    nc = await asyncio.wait_for(nats.connect(url, connect_timeout=3), timeout=5)
    try:
        js = nc.jetstream()
        # 갓 뜬(또는 마커 있는) 테스트 서버 — 통과하고 마커가 서 있다
        await assert_test_nats(js)
        await js.stream_info(TEST_MARKER_STREAM)
        # 두 번째 호출은 마커 경로 — LF_* 스트림이 생겨도 마커가 이긴다
        await assert_test_nats(js)
    finally:
        await nc.close()


def test_nats_guard_marks_and_accepts_test_server():
    url = test_nats_url()
    if url is None:
        pytest.skip("LF_TEST_NATS_URL 미설정 — NATS 가드 통합 검증 생략")
    asyncio.run(_nats_guard_roundtrip(url))
