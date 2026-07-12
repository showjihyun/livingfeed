"""feed composer 엔트리포인트 (ADR-014 §1단).

실행:
    uv run --package lf-feed python -m lf_feed.main
설정: LF_PG_DSN, NATS_URL, LF_ENV, LF_PERSONAS_DIR, LF_FEED_THRESHOLD (config.py 참고).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from lf_feed.composer import FeedComposer
from lf_feed.config import Config


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await FeedComposer(Config.from_env()).run(stop=stop)


def main() -> None:
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
