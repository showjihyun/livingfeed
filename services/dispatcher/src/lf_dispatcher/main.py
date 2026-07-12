"""dispatcher 서비스 엔트리포인트 — outbox relay (ADR-017).

실행:
    uv run --package lf-dispatcher python -m lf_dispatcher.main
설정은 환경변수로: LF_PG_DSN, NATS_URL, LF_ENV (config.py 참고).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from lf_dispatcher.config import Config
from lf_dispatcher.relay import run_relay


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows 로컬 실행 등 add_signal_handler 미지원 환경은 Ctrl+C(KeyboardInterrupt)로 종료
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await run_relay(Config.from_env(), stop=stop)


def main() -> None:
    # Windows 콘솔(cp949)에서도 한글 로그가 깨지지 않도록
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if sys.platform == "win32":
        # psycopg async는 ProactorEventLoop 미지원 (로컬 개발 전용 — prod는 Linux)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())


if __name__ == "__main__":
    main()
