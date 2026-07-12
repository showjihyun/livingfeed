"""API Gateway — 피드 SSE 스트림 + 롱폴링 폴백 (ADR-010).

- GET /stream/feed : SSE. Last-Event-ID(또는 cursor 파라미터)로 이어받기.
  이벤트 id = 피드 커서(ULID) — 놓친 이벤트 문제는 페이지네이션과 같은 메커니즘.
- GET /poll/feed   : SSE 불가 환경의 롱폴링 자동 강등 대상.
- WSS /session     : 상호작용 세션 — 메일박스 상호작용 경로(ADR-012 후속)와 함께 구현된다.

gateway는 무상태다: 연결당 임시 JetStream consumer만 만들고, 재개 좌표는
전적으로 클라이언트 커서에 있다 (수평 확장 자유, ADR-010/019).

실행: uv run --package lf-gateway uvicorn lf_gateway.main:app --reload
설정: NATS_URL, LF_ENV (config.py 참고).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager

import nats
import nats.errors
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from lf_gateway.config import Config
from lf_gateway.feed_stream import (
    FEED_KINDS,
    FeedFilter,
    open_feed_subscription,
    parse_feed_event,
    sse_frames,
)

logger = logging.getLogger("lf.gateway.main")

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # nginx 계열 프록시의 응답 버퍼링 무효화 — SSE 지연 방지 (ADR-019)
    "X-Accel-Buffering": "no",
}


def _parse_filter(world_id: str, types: str, cursor: str | None) -> FeedFilter:
    kinds = frozenset(t.strip() for t in types.split(",") if t.strip())
    unknown = kinds - FEED_KINDS
    if not kinds or unknown:
        raise HTTPException(400, f"알 수 없는 피드 유형: {sorted(unknown) or types!r}")
    if cursor is not None and not ULID_RE.match(cursor):
        raise HTTPException(400, "커서는 post id(ULID)여야 한다")
    return FeedFilter(world_id=world_id, kinds=kinds, cursor=cursor)


def create_app(cfg: Config | None = None, nc: nats.NATS | None = None) -> FastAPI:
    """앱 팩토리 — 테스트는 nc(NATS 연결)를 주입한다. 미주입이면 lifespan이 만들고 닫는다."""
    cfg = cfg or Config.from_env()
    owned_nc = nc is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        connection = nc if nc is not None else await nats.connect(cfg.nats_url)
        app.state.js = connection.jetstream()
        try:
            yield
        finally:
            if owned_nc:
                await connection.drain()

    app = FastAPI(title="lf-gateway", lifespan=lifespan)
    # 브라우저 EventSource/fetch — 웹 앱(기본 localhost:3000)의 교차 출처 허용
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.cors_origins),
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    if not owned_nc:
        app.state.js = nc.jetstream()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "lf-gateway"}

    @app.get("/stream/feed")
    async def stream_feed(
        world_id: str = Query("w_main", pattern=r"^w_[a-z0-9_]+$"),
        types: str = Query("world"),
        cursor: str | None = Query(None),
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        # 브라우저 EventSource 재접속은 Last-Event-ID 헤더로 온다 — 파라미터보다 우선
        flt = _parse_filter(world_id, types, last_event_id or cursor)
        return StreamingResponse(
            sse_frames(app.state.js, cfg, flt),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    @app.get("/poll/feed")
    async def poll_feed(
        world_id: str = Query("w_main", pattern=r"^w_[a-z0-9_]+$"),
        types: str = Query("world"),
        cursor: str | None = Query(None),
        wait_s: float = Query(5.0, ge=0.0),
    ) -> dict:
        """롱폴링 폴백 — 첫 아이템까지 최대 wait_s 대기, 이후 도착분을 묶어 반환한다."""
        flt = _parse_filter(world_id, types, cursor)
        wait_s = min(wait_s, cfg.poll_max_wait_s)

        items: list[str] = []
        next_cursor = cursor
        sub = await open_feed_subscription(app.state.js, cfg, flt)
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait_s
            while len(items) < cfg.poll_batch:
                # 첫 아이템은 데드라인까지, 이후는 짧게 — 이미 도착한 분만 마저 묶는다
                timeout = 0.2 if items else max(deadline - loop.time(), 0.05)
                try:
                    msg = await sub.next_msg(timeout=timeout)
                except (TimeoutError, nats.errors.TimeoutError):
                    if items or loop.time() >= deadline:
                        break
                    continue
                parsed = parse_feed_event(msg.data, flt)
                if parsed is not None:
                    items.append(parsed[2])
                    next_cursor = parsed[0]
        finally:
            await sub.unsubscribe()

        # items는 봉투 JSON 원문 — 파싱 1회로 반환 구조에 싣는다
        return {"items": [json.loads(i) for i in items], "next_cursor": next_cursor}

    return app


app = create_app()
