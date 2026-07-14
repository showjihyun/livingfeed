"""director 엔트리포인트 (ADR-013).

실행:
    uv run --package lf-director python -m lf_director.main
설정: LF_PG_DSN, NATS_URL, LF_ENV, LF_WORLD_ID, LF_DIRECTOR_QUIET_TICKS (config.py 참고).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

import yaml

from lf_director.config import Config
from lf_director.director import Director

logger = logging.getLogger("lf.director")


def load_actor_names(personas_dir: Path) -> dict[str, str]:
    """페르소나 id → 표시 이름 — 개입 서술 그라운딩용. 없으면 빈 매핑(id로 표기)."""
    names: dict[str, str] = {}
    if not personas_dir.is_dir():
        logger.warning("페르소나 디렉터리 없음: %s — 액터 id로 표기한다", personas_dir)
        return names
    for path in sorted(personas_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            names[doc["id"]] = doc["name"]
        except Exception:  # 페르소나 파일 손상이 Director를 멈추면 안 된다
            logger.warning("페르소나 파싱 실패: %s — 건너뜀", path, exc_info=True)
    return names


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    cfg = Config.from_env()
    names = load_actor_names(cfg.personas_dir)
    await Director(cfg, names=names).run(stop=stop)


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
