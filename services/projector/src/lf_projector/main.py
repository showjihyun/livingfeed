"""projector 엔트리포인트 (ADR-003).

실행:
    uv run --package lf-projector python -m lf_projector.main --kind os
    uv run --package lf-projector python -m lf_projector.main --kind kuzu
    uv run --package lf-projector python -m lf_projector.main --kind os --rebuild
설정: NATS_URL, OPENSEARCH_URL, LF_KUZU_DIR, LF_ENV (config.py 참고).

pg/qdrant/redis 프로젝터는 각자의 로드맵 단계에서 추가된다.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

from lf_projector.config import Config
from lf_projector.kuzu_projector import KuzuProjector
from lf_projector.os_projector import OsProjector

KINDS = ("os", "kuzu")


async def run(kind: str, rebuild: bool) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    cfg = Config.from_env()
    if kind == "kuzu":
        await KuzuProjector(cfg).run(stop=stop, rebuild=rebuild)
    else:
        await OsProjector(cfg).run(stop=stop, rebuild=rebuild)


def main() -> None:
    parser = argparse.ArgumentParser(description="Living Feed projection worker")
    parser.add_argument("--kind", choices=KINDS, default="os")
    parser.add_argument(
        "--rebuild", action="store_true",
        help="프로젝션 파괴 후 이벤트 로그 처음부터 재구축 (ADR-003 계약 3)",
    )
    args = parser.parse_args()

    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run(args.kind, args.rebuild))


if __name__ == "__main__":
    main()
