"""피드 조회 API — 배치 2단의 전달 경로 + 읽기 모델 조회 (ADR-014 §2단, ADR-003).

프로젝션만 읽는다 (ADR-003 읽기 API 규칙):
- GET /feed: World/Community 등은 OpenSearch(fan-out-on-read + Redis 캐시 30s),
  Personal/Private는 Redis 타임라인(fan-out-on-write, redis-projector 산출물)
- GET /actors/{id}/profile, GET /messages: PG read 테이블(pg-projector 산출물)
플레이어 인증·개인화(Personal/Private 접근 제어)는 gateway 인증 단계의 후속이다.

실행: uvicorn lf_feed_api.main:app --reload
(설정: OPENSEARCH_URL, REDIS_URL, LF_DATABASE_URL)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from contextlib import asynccontextmanager

import nats
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from lf_projector.graph_api import GraphQueryClient
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from lf_feed_api.config import Config
from lf_feed_api.reads import ProfileReads
from lf_feed_api.search import FeedSearch
from lf_feed_api.timeline import TIMELINE_KINDS, read_timeline

logger = logging.getLogger("lf.feed_api.main")

# psycopg async는 Windows ProactorEventLoop에서 동작하지 않는다 (로컬 개발 전용).
# 루프가 만들어지기 전에 정책이 서야 하므로 uvicorn CLI로는 부족하다 —
# Windows에서는 `python -m lf_feed_api.main`(아래 main)으로 실행하라.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
FEED_KINDS = frozenset({"world", "community", "relationship", "personal", "private", "hidden"})


def create_app(
    cfg: Config | None = None,
    search: FeedSearch | None = None,
    cache: Redis | None = None,
    graph: GraphQueryClient | None = None,
    reads: ProfileReads | None = None,
) -> FastAPI:
    """앱 팩토리 — 테스트는 search/cache/graph/reads를 주입한다. 미주입분은 lifespan이 만들고 닫는다."""
    cfg = cfg or Config.from_env()

    owned_search = search is None
    owned_cache = cache is None
    owned_graph = graph is None
    owned_reads = reads is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nc = None
        pool = None
        if owned_search:
            app.state.search = FeedSearch(cfg)
        if owned_cache:
            app.state.cache = Redis.from_url(cfg.redis_url)
        if owned_graph:
            # 근접도는 최적화다 — NATS 미가용이 읽기 경로를 죽이면 안 된다
            try:
                nc = await nats.connect(cfg.nats_url, connect_timeout=2)
                app.state.graph = GraphQueryClient(nc, cfg.env)
            except Exception as e:
                logger.warning("graph query 미가용(근접도 항 비활성): %s", e)
                app.state.graph = None
        if owned_reads:
            # PG 미가용이 /feed를 죽이면 안 된다 — 프로필/대화 경로만 503으로 격리
            try:
                pool = AsyncConnectionPool(
                    cfg.database_url, min_size=1, max_size=4, open=False
                )
                await pool.open(wait=True, timeout=5)
                app.state.reads = ProfileReads(pool)
            except Exception as e:
                logger.warning("PG read 미가용(프로필/대화 503): %s", e)
                pool = None
                app.state.reads = None
        try:
            yield
        finally:
            if owned_search:
                await app.state.search.close()
            if owned_cache:
                await app.state.cache.aclose()
            if nc is not None:
                await nc.drain()
            if pool is not None:
                await pool.close()

    app = FastAPI(title="lf-feed-api", lifespan=lifespan)
    # 브라우저 fetch — 웹 앱(기본 localhost:3000)의 교차 출처 허용
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.cors_origins),
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    # 주입분(테스트)은 즉시 배선 — lifespan은 소유분만 만들고 닫는다
    if not owned_search:
        app.state.search = search
    if not owned_cache:
        app.state.cache = cache
    if not owned_graph:
        app.state.graph = graph
    if not owned_reads:
        app.state.reads = reads

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "lf-feed-api"}

    @app.get("/feed")
    async def get_feed(
        world_id: str = Query("w_main", pattern=r"^w_[a-z0-9_]+$"),
        types: str = Query("world", description="쉼표 구분 가시성 등급 (ADR-014 6등급)"),
        cursor: str | None = Query(None, description="post id(ULID) — 이후 recent 이어보기"),
        limit: int = Query(cfg.default_limit, ge=1),
        sort: str = Query("ranked", description="ranked=랭킹 첫 화면, recent=시간순"),
        player_id: str | None = Query(
            None, pattern=r"^p_[a-z0-9_]+$",
            description="랭킹에 관계 근접도 항을 켠다 (ADR-014 w_proximity)",
        ),
    ) -> dict:
        kinds = sorted({t.strip() for t in types.split(",") if t.strip()})
        unknown = set(kinds) - FEED_KINDS
        if not kinds or unknown:
            raise HTTPException(400, f"알 수 없는 피드 유형: {sorted(unknown) or types!r}")
        if sort not in ("ranked", "recent"):
            raise HTTPException(400, f"sort는 ranked|recent — 받은 값: {sort!r}")
        if cursor is not None:
            if not ULID_RE.match(cursor):
                raise HTTPException(400, "cursor는 ULID여야 한다 (post id)")
            sort = "recent"  # 커서 이어보기는 정의상 시간순이다 (search.py 참고)
        limit = min(limit, cfg.max_limit)

        # Personal/Private — 플레이어 단위 타임라인 (fan-out-on-write, ADR-014 §2단).
        # 저장소가 다르므로(OS가 아니라 Redis) 다른 등급과 섞어 질의할 수 없다.
        if set(kinds) & TIMELINE_KINDS:
            if not set(kinds) <= TIMELINE_KINDS:
                raise HTTPException(
                    400, "personal/private는 다른 등급과 섞어 질의할 수 없다 (타임라인 경로)"
                )
            if player_id is None:
                raise HTTPException(400, "personal/private 피드는 player_id가 필요하다")
            return await read_timeline(
                app.state.cache, world_id, player_id, kinds, limit=limit, cursor=cursor
            )

        graph = getattr(app.state, "graph", None)
        personalize = sort == "ranked" and player_id is not None and graph is not None

        cache_key = None
        if cursor is None and not personalize:
            # 첫 화면만 캐시한다 — 개인화 응답은 공유 캐시에 넣지 않는다 (ADR-014)
            cache_key = f"feed:{world_id}:{','.join(kinds)}:{sort}:{limit}"
            cached = await _cache_get(app.state.cache, cache_key)
            if cached is not None:
                return cached

        # 개인화 재랭킹은 후보를 넉넉히 뽑아 근접도 항을 더한 뒤 자른다
        fetch_limit = min(limit * 2, cfg.max_limit) if personalize else limit
        result = await app.state.search.search(
            world_id, kinds, limit=fetch_limit, sort=sort, cursor=cursor
        )

        if personalize and result["items"]:
            authors = sorted({item["actor_id"] for item in result["items"]})
            proximity = await graph.proximity(world_id, player_id, authors)
            if proximity:
                # score = OS(0.4·drama + 0.2·시간감쇠) + 0.25·관계근접도 (ADR-014)
                for item in result["items"]:
                    item["_score"] = round(
                        item.get("_score", 0.0)
                        + cfg.w_proximity * proximity.get(item["actor_id"], 0.0),
                        6,
                    )
                result["items"].sort(key=lambda item: -item["_score"])
                result["personalized"] = True
            result["items"] = result["items"][:limit]

        if cache_key is not None:
            await _cache_set(app.state.cache, cache_key, result, cfg.cache_ttl_s)
        return result

    def _reads() -> ProfileReads:
        reads = getattr(app.state, "reads", None)
        if reads is None:
            raise HTTPException(503, "read 프로젝션 미가용 — PG와 pg-projector를 확인하라")
        return reads

    def _validate_cursor(cursor: str | None) -> str | None:
        if cursor is not None and not ULID_RE.match(cursor):
            raise HTTPException(400, "cursor는 ULID여야 한다 (event id)")
        return cursor

    @app.get("/actors/{actor_id}/profile")
    async def actor_profile(
        actor_id: str = Path(pattern=r"^a_[a-z0-9_]+$"),
        world_id: str = Query("w_main", pattern=r"^w_[a-z0-9_]+$"),
        episode_limit: int = Query(20, ge=1),
        episode_cursor: str | None = Query(None, description="에피소드 이어보기 (event id ULID)"),
    ) -> dict:
        """액터의 내면 — 신념 전체 + 최근 에피소드 (ADR-008이 제품에서 드러나는 곳)."""
        return await _reads().actor_profile(
            world_id, actor_id,
            episode_limit=min(episode_limit, cfg.max_limit),
            episode_cursor=_validate_cursor(episode_cursor),
        )

    @app.get("/messages")
    async def conversation(
        player_id: str = Query(pattern=r"^p_[a-z0-9_]+$"),
        actor_id: str = Query(pattern=r"^a_[a-z0-9_]+$"),
        world_id: str = Query("w_main", pattern=r"^w_[a-z0-9_]+$"),
        limit: int = Query(50, ge=1),
        cursor: str | None = Query(None, description="과거 방향 이어보기 (event id ULID)"),
    ) -> dict:
        """플레이어↔액터 대화 히스토리 — WS 재접속 이어보기 (ADR-012 상호작용 경로).

        player_id는 아직 클라이언트 주장 값이다 — 진짜 접근 제어는 인증 단계의
        후속 (WS 세션과 같은 보안 캐비앳).
        """
        return await _reads().conversation(
            world_id, player_id, actor_id,
            limit=min(limit, cfg.max_limit),
            cursor=_validate_cursor(cursor),
        )

    return app


async def _cache_get(cache: Redis, key: str) -> dict | None:
    """캐시는 최적화일 뿐이다 — Redis 장애가 읽기 경로를 죽이면 안 된다 (명시 로그 후 우회)."""
    try:
        raw = await cache.get(key)
    except Exception as e:
        logger.warning("캐시 조회 실패(우회): %s", e)
        return None
    return json.loads(raw) if raw is not None else None


async def _cache_set(cache: Redis, key: str, value: dict, ttl_s: int) -> None:
    try:
        await cache.setex(key, ttl_s, json.dumps(value, ensure_ascii=False))
    except Exception as e:
        logger.warning("캐시 저장 실패(우회): %s", e)


app = create_app()


def main() -> None:
    """로컬 실행 진입점 — 루프를 직접 만든다 (uvicorn.run은 win32에서 Proactor를 강제)."""
    import os

    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
    )
    asyncio.run(server.serve())  # 위 정책(win32=Selector)의 루프에서 psycopg가 돈다


if __name__ == "__main__":
    main()
