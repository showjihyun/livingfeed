"""tick engine 워커 엔트리포인트 (ADR-011, ADR-001 규칙 2의 engine/tick 배포 단위).

실행:
    uv run --package lf-tick python -m lf_tick.main
설정: LF_PG_DSN, LF_WORLD_ID, LF_GENESIS, LF_REAL_SECONDS_PER_TICK (config.py 참고).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from lf_tick.config import TickConfig
from lf_tick.engine import run_tick_loop
from lf_tick.pipeline import NoopPhases


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await run_tick_loop(TickConfig.from_env(), NoopPhases(), stop=stop)


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
