import asyncio
import os
import sys

import pytest

# psycopg 계열 규약과 동일 — Windows 로컬 개발 배려 (Selector 이벤트 루프)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

NATS_URL = os.environ.get("LF_TEST_NATS_URL", "nats://localhost:4222")


@pytest.fixture
async def nc():
    """NATS 연결 + 스트림 초기화. 미가용이면 skip (CI는 fail) — dispatcher conftest 규약."""
    import nats
    from lf_dispatcher.streams import STREAMS, ensure_streams
    from nats.js.errors import NotFoundError

    try:
        connection = await asyncio.wait_for(nats.connect(NATS_URL, connect_timeout=3), timeout=5)
    except Exception:
        if "LF_TEST_NATS_URL" in os.environ:
            raise
        pytest.skip(f"NATS 미가용 ({NATS_URL}) — infra/compose에서 nats를 켜라")
    try:
        js = connection.jetstream()
        for spec in STREAMS:  # 테스트 격리: 이전 실행의 메시지 제거
            try:
                await js.delete_stream(spec.name)
            except NotFoundError:
                pass
        await ensure_streams(js)
        yield connection
    finally:
        await connection.close()
