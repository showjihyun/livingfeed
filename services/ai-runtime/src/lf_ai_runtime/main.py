"""AI Runtime 서비스 엔트리포인트 (ADR-018).

실행:
    uv run --package lf-ai-runtime python -m lf_ai_runtime.main
설정: NATS_URL, LF_ENV, LF_AI_PROVIDER(rule|anthropic), LF_MODEL_ROUTES,
LF_AI_CONCURRENCY(동시 처리 상한, 기본 4) — config.py 참고.
anthropic 프로바이더는 SDK 표준 자격증명 해석(ANTHROPIC_API_KEY 등)을 따른다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from lf_ai_runtime.config import Config
from lf_ai_runtime.service import serve


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await serve(Config.from_env(), stop=stop)


def main() -> None:
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())


if __name__ == "__main__":
    main()
