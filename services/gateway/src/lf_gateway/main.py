"""API Gateway — 피드 SSE 스트림 + 롱폴링 폴백 (ADR-010).

- GET /stream/feed : SSE. Last-Event-ID(또는 cursor 파라미터)로 이어받기.
  이벤트 id = 피드 커서(ULID) — 놓친 이벤트 문제는 페이지네이션과 같은 메커니즘.
- GET /poll/feed   : SSE 불가 환경의 롱폴링 자동 강등 대상.
- WSS /session     : 상호작용 세션 — {type, seq, payload} 봉투로 dm.send /
  comment.post / reaction.add 커맨드를 받아 player.* 이벤트로 적재하고,
  액터 응답(actor.message.sent)을 push한다 (session.py, ADR-012 메일박스 경로).

gateway는 무상태다: 연결당 임시 JetStream consumer만 만들고, 재개 좌표는
전적으로 클라이언트 커서에 있다 (수평 확장 자유, ADR-010/019).

실행: uv run --package lf-gateway uvicorn lf_gateway.main:app --reload
설정: NATS_URL, LF_ENV (config.py 참고).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import sys
from contextlib import asynccontextmanager

import nats
import nats.errors
from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from lf_projector.graph_api import GraphQueryClient
from psycopg import AsyncConnection
from redis.asyncio import Redis

from lf_gateway.config import Config
from lf_gateway.feed_stream import (
    FEED_KINDS,
    FeedFilter,
    open_feed_subscription,
    parse_feed_event,
    sse_frames,
)
from lf_gateway.session import (
    PLAYER_RE,
    handle_command,
    open_reply_subscription,
    presence_key,
    presence_value,
    reply_frame_for,
)

logger = logging.getLogger("lf.gateway.main")

# psycopg async는 Windows ProactorEventLoop에서 동작하지 않는다 (WS 세션이 커맨드
# 적재에 psycopg를 쓴다). 루프가 만들어지기 전에 정책이 서야 하므로 uvicorn CLI로는
# 부족하다 — Windows 로컬은 `python -m lf_gateway.main`(아래 main)으로 실행하라.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

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
        app.state.graph = GraphQueryClient(connection, cfg.env)
        app.state.redis = Redis.from_url(cfg.redis_url)
        try:
            yield
        finally:
            await app.state.redis.aclose()
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
        app.state.graph = GraphQueryClient(nc, cfg.env)

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

    @app.get("/graph/relationships")
    async def graph_relationships(
        world_id: str = Query("w_main", pattern=r"^w_[a-z0-9_]+$"),
        player_id: str = Query(..., pattern=r"^p_[a-z0-9_]+$"),
    ) -> dict:
        """플레이어 관계 그래프의 실측값 — kuzu-projector의 graph query API 중재 (ADR-006).

        그래프 미가용은 오류가 아니다(프로젝션은 최적화) — available=False로
        내려주고 FE는 데모 배치를 유지한다.
        """
        output = await app.state.graph.player_graph(world_id, player_id)
        if output is None:
            return {"player_id": player_id, "edges": [], "available": False}
        return {**output, "available": True}

    @app.websocket("/session")
    async def session(ws: WebSocket) -> None:
        # ⚠️ 인증 경계: player_id는 아직 클라이언트 주장 값이다 — 계정 체계(후속)가
        # 생기면 검증된 신원에서 도출한다. 그전에 로컬 밖에 노출하려면
        # LF_SESSION_TOKEN(공유 토큰 게이트)을 반드시 설정하라 (config.py).
        if cfg.session_token is not None:
            # 브라우저 EventSource와 달리 WS 클라이언트는 헤더를 실을 수 있다 —
            # ?token=(웹 앱)과 Authorization: Bearer(비브라우저) 둘 다 허용.
            supplied = ws.query_params.get("token")
            if supplied is None:
                scheme, _, credential = ws.headers.get("authorization", "").partition(" ")
                supplied = credential.strip() if scheme.lower() == "bearer" else ""
            if not hmac.compare_digest(supplied, cfg.session_token):
                # accept 전 close — 핸드셰이크 자체를 거부한다 (1008 = 정책 위반, RFC 6455)
                await ws.close(code=1008, reason="세션 토큰이 필요하다 (LF_SESSION_TOKEN)")
                return
        player_id = ws.query_params.get("player_id", "")
        world_id = ws.query_params.get("world_id", "w_main")
        if not PLAYER_RE.match(player_id) or not re.match(r"^w_[a-z0-9_]+$", world_id):
            await ws.close(code=4400, reason="player_id(p_*)와 world_id(w_*)가 필요하다")
            return
        await ws.accept()

        send_lock = asyncio.Lock()  # 커맨드 응답과 push가 같은 소켓을 쓴다

        async def push_replies() -> None:
            sub = await open_reply_subscription(app.state.js, cfg, world_id)
            server_seq = 0
            try:
                while True:
                    try:
                        msg = await sub.next_msg(timeout=cfg.heartbeat_s)
                    except (TimeoutError, nats.errors.TimeoutError):
                        continue
                    frame = reply_frame_for(msg.data, player_id, server_seq)
                    if frame is not None:
                        server_seq += 1
                        async with send_lock:
                            await ws.send_json(frame)
            finally:
                await sub.unsubscribe()

        push_task = asyncio.create_task(push_replies())
        conn = await AsyncConnection.connect(cfg.pg_dsn, autocommit=True)
        try:
            while True:
                raw = await ws.receive_json()
                response = await handle_command(conn, world_id, player_id, raw)
                async with send_lock:
                    await ws.send_json(response)
                try:  # 프레즌스는 최적화 — Redis 장애가 세션을 죽이면 안 된다
                    await app.state.redis.set(
                        presence_key(world_id, player_id), presence_value(raw.get("seq")),
                        ex=3600,
                    )
                except Exception as e:
                    logger.warning("프레즌스 저장 실패(무시): %s", e)
        except WebSocketDisconnect:
            pass
        finally:
            push_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await push_task
            await conn.close()

    return app


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
