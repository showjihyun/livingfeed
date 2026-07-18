"""projector 엔트리포인트 (ADR-003).

실행:
    uv run --package lf-projector python -m lf_projector.main --kind os
    uv run --package lf-projector python -m lf_projector.main --kind kuzu
    uv run --package lf-projector python -m lf_projector.main --kind pg
    uv run --package lf-projector python -m lf_projector.main --kind redis
    uv run --package lf-projector python -m lf_projector.main --kind os --rebuild --once
    uv run --package lf-projector python -m lf_projector.main --kind pg --rebuild --from-es
    uv run --package lf-projector python -m lf_projector.main --kind kuzu --verify [--world w_x]
설정: NATS_URL, OPENSEARCH_URL, LF_KUZU_DIR, LF_DATABASE_URL, REDIS_URL, LF_ENV
(config.py 참고). LF_METRICS_PORT를 주면 Prometheus 지표(/metrics)를 함께
노출한다 — 미설정이 기본(로그만). qdrant 프로젝터는 자신의 로드맵 단계에서
추가된다.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from pathlib import Path

from prometheus_client import REGISTRY, CollectorRegistry, start_http_server
from psycopg import AsyncConnection
from redis.asyncio import Redis

from lf_projector.config import Config
from lf_projector.graph import RelGraph
from lf_projector.kuzu_projector import KuzuProjector
from lf_projector.kuzu_verify import verify_worlds
from lf_projector.lag import LagMetrics
from lf_projector.os_index import OpenSearchIndex, envelope_to_doc
from lf_projector.os_projector import RETIRED_TYPE as OS_RETIRED_TYPE
from lf_projector.os_projector import OsProjector
from lf_projector.pg_projector import PgProjector
from lf_projector.pg_read import ReadStore
from lf_projector.pg_verify import verify_pg
from lf_projector.redis_projector import RedisProjector
from lf_projector.replay import PATTERNS, replay_envelopes, replay_into
from lf_projector.timeline import TimelineStore
from lf_projector.timeline_verify import verify_timeline

logger = logging.getLogger("lf.projector.main")

KINDS = ("os", "kuzu", "pg", "redis")

PROJECTORS = {
    "os": OsProjector,
    "kuzu": KuzuProjector,
    "pg": PgProjector,
    "redis": RedisProjector,
}


def start_metrics_server(registry: CollectorRegistry | None = None) -> LagMetrics | None:
    """LF_METRICS_PORT가 있을 때만 Prometheus 지표 서버를 켠다 — dev 기본은 로그만.

    한 호스트에 프로젝터 넷이 뜨므로 kind별 포트는 운영자가 나눠 준다
    (infra/compose/README.md 포트 표, 예: 9101~9104). 수집기(Prometheus 서버)는
    운영 환경의 몫이다. registry 주입은 테스트 격리용.
    """
    port = os.environ.get("LF_METRICS_PORT")
    if not port:
        return None
    registry = REGISTRY if registry is None else registry
    metrics = LagMetrics(registry=registry)  # 서버보다 먼저 — 첫 스크레이프부터 지표면이 있다
    start_http_server(int(port), registry=registry)
    logger.info("metrics 노출 — :%d/metrics", int(port))
    return metrics


async def run(kind: str, rebuild: bool, once: bool) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    cfg = Config.from_env()
    metrics = start_metrics_server()
    kind_metrics = None if metrics is None else metrics.for_kind(kind)
    await PROJECTORS[kind](cfg, metrics=kind_metrics).run(stop=stop, rebuild=rebuild, once=once)


async def run_from_es(kind: str) -> int:
    """SoT(es.events) 리플레이로 프로젝션을 완전 재구축한다 — NATS 불요.

    JetStream 보존 한도 밖의 역사까지 되세운다. 해당 프로젝터 서비스는 멈춘
    상태를 상정한다 (재기동 시 durable 체크포인트부터 이어가고, 리플레이와의
    겹침은 프로젝션 멱등이 흡수한다).
    """
    cfg = Config.from_env()
    async with await AsyncConnection.connect(cfg.database_url, autocommit=True) as conn:
        if kind == "pg":
            store = ReadStore(conn)
            await store.drop()
            await store.ensure()
            fed = await replay_into(conn, PATTERNS["pg"], store.apply)
        elif kind == "kuzu":
            projector = KuzuProjector(cfg)
            try:
                projector.graph.drop_all()
                fed = await replay_into(conn, PATTERNS["kuzu"], projector.replay_apply())
            finally:
                projector.graph.close()
        elif kind == "redis":
            redis = Redis.from_url(cfg.redis_url)
            try:
                store = TimelineStore(redis)
                await store.drop_all()
                fed = await replay_into(
                    conn, PATTERNS["redis"], RedisProjector(cfg).replay_apply(store)
                )
            finally:
                await redis.aclose()
        else:  # os
            index = OpenSearchIndex(cfg.opensearch_url, cfg.index)
            try:
                await index.drop()
                await index.ensure()
                fed, docs = 0, []
                async for envelope in replay_envelopes(conn, PATTERNS["os"]):
                    fed += 1
                    if envelope["type"] == OS_RETIRED_TYPE:
                        # 순서가 곧 결정성이다 — 쌓인 색인을 먼저 밀고 소멸을 집행한다
                        await index.bulk_upsert(docs)
                        docs = []
                        await index.delete_by_actor(
                            envelope["world_id"], envelope["payload"]["actor_id"]
                        )
                        continue
                    docs.append(envelope_to_doc(envelope))
                    if len(docs) >= 500:
                        await index.bulk_upsert(docs)
                        docs = []
                if docs:
                    await index.bulk_upsert(docs)
            finally:
                await index.close()
    print(json.dumps({"kind": kind, "replayed": fed}, ensure_ascii=False))
    return 0


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
        "--once", action="store_true",
        help="스트림이 유휴해질 때까지 소비하고 종료 — --rebuild와 함께 일회성 배치",
    )
    parser.add_argument(
        "--from-es", action="store_true",
        help="SoT(es.events) 리플레이로 완전 재구축 — NATS 불요·보존 한도 무관, --rebuild 필수",
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
    if args.from_es:
        if not args.rebuild or args.once:
            parser.error("--from-es 는 --rebuild 와 함께만 쓴다 (--once 와 배타 — NATS 불요)")
        raise SystemExit(asyncio.run(run_from_es(args.kind)))
    asyncio.run(run(args.kind, args.rebuild, args.once))


if __name__ == "__main__":
    main()
