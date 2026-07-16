"""projector 엔트리포인트 (ADR-003).

실행:
    uv run --package lf-projector python -m lf_projector.main --kind os
    uv run --package lf-projector python -m lf_projector.main --kind kuzu
    uv run --package lf-projector python -m lf_projector.main --kind pg
    uv run --package lf-projector python -m lf_projector.main --kind redis
    uv run --package lf-projector python -m lf_projector.main --kind os --rebuild
    uv run --package lf-projector python -m lf_projector.main --kind kuzu --verify [--world w_x]
설정: NATS_URL, OPENSEARCH_URL, LF_KUZU_DIR, LF_DATABASE_URL, REDIS_URL, LF_ENV
(config.py 참고). qdrant 프로젝터는 자신의 로드맵 단계에서 추가된다.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from pathlib import Path

from psycopg import AsyncConnection
from redis.asyncio import Redis

from lf_projector.config import Config
from lf_projector.graph import RelGraph
from lf_projector.kuzu_projector import KuzuProjector
from lf_projector.kuzu_verify import verify_worlds
from lf_projector.os_projector import OsProjector
from lf_projector.pg_projector import PgProjector
from lf_projector.pg_verify import verify_pg
from lf_projector.redis_projector import RedisProjector
from lf_projector.timeline_verify import verify_timeline

KINDS = ("os", "kuzu", "pg", "redis")

PROJECTORS = {
    "os": OsProjector,
    "kuzu": KuzuProjector,
    "pg": PgProjector,
    "redis": RedisProjector,
}


async def run(kind: str, rebuild: bool) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    cfg = Config.from_env()
    await PROJECTORS[kind](cfg).run(stop=stop, rebuild=rebuild)


async def run_verify(kind: str, world: str | None) -> int:
    """프로젝션 무결성 검사 — 검사만 하고 고치지 않는다 (주간 배치의 체크 커맨드).

    kuzu는 그래프 엣지 집합, pg는 read 테이블 키/개수, redis는 팔로워 인덱스를
    원천(es) 대비 비교한다. 어긋남은 --rebuild 판단 근거다.
    """
    cfg = Config.from_env()
    async with await AsyncConnection.connect(cfg.database_url, autocommit=True) as conn:
        if kind == "kuzu":
            graph = RelGraph(Path(cfg.kuzu_dir))
            try:
                report = await verify_worlds(conn, graph, world_id=world)
            finally:
                graph.close()
        elif kind == "pg":
            report = await verify_pg(conn, world_id=world)
        else:  # redis
            redis = Redis.from_url(cfg.redis_url)
            try:
                report = await verify_timeline(conn, redis, world_id=world)
            finally:
                await redis.aclose()
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Living Feed projection worker")
    parser.add_argument("--kind", choices=KINDS, default="os")
    parser.add_argument(
        "--rebuild", action="store_true",
        help="프로젝션 파괴 후 이벤트 로그 처음부터 재구축 (ADR-003 계약 3)",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="원천(es) 대비 프로젝션 무결성 검사(kuzu/pg/redis) — 어긋나면 종료 코드 1",
    )
    parser.add_argument("--world", default=None, help="--verify 대상 세계 (기본: 전 세계)")
    args = parser.parse_args()

    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if args.verify:
        if args.kind not in ("kuzu", "pg", "redis"):
            parser.error("--verify 는 kuzu/pg/redis 전용이다 (os는 색인 재구축으로 갈음)")
        raise SystemExit(asyncio.run(run_verify(args.kind, args.world)))
    asyncio.run(run(args.kind, args.rebuild))


if __name__ == "__main__":
    main()
