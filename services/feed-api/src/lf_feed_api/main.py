"""피드 조회 API — 배치 2단의 전달 경로 (ADR-014 §2단).

GET /feed: 프로젝션(OpenSearch)만 읽는다 (ADR-003 읽기 API 규칙).
fan-out-on-read + Redis 결과 캐시(30s TTL)로 p95 < 200ms를 지킨다 (ADR-020).
플레이어 인증·개인화(Personal/Private 접근 제어)는 gateway 인증 단계의 후속이다.

실행: uvicorn lf_feed_api.main:app --reload  (설정: OPENSEARCH_URL, REDIS_URL)
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from lf_feed_api.config import Config
from lf_feed_api.search import FeedSearch

logger = logging.getLogger("lf.feed_api.main")

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
FEED_KINDS = frozenset({"world", "community", "relationship", "personal", "private", "hidden"})


def create_app(
    cfg: Config | None = None,
    search: FeedSearch | None = None,
    cache: Redis | None = None,
) -> FastAPI:
    """앱 팩토리 — 테스트는 search/cache를 주입한다. 미주입분은 lifespan이 만들고 닫는다."""
    cfg = cfg or Config.from_env()

    owned_search = search is None
    owned_cache = cache is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if owned_search:
            app.state.search = FeedSearch(cfg)
        if owned_cache:
            app.state.cache = Redis.from_url(cfg.redis_url)
        try:
            yield
        finally:
            if owned_search:
                await app.state.search.close()
            if owned_cache:
                await app.state.cache.aclose()

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

        cache_key = None
        if cursor is None:
            # 첫 화면만 캐시한다 — fan-out-on-read 부하의 대부분 (ADR-014)
            cache_key = f"feed:{world_id}:{','.join(kinds)}:{sort}:{limit}"
            cached = await _cache_get(app.state.cache, cache_key)
            if cached is not None:
                return cached

        result = await app.state.search.search(
            world_id, kinds, limit=limit, sort=sort, cursor=cursor
        )
        if cache_key is not None:
            await _cache_set(app.state.cache, cache_key, result, cfg.cache_ttl_s)
        return result

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
