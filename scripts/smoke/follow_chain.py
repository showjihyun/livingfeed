"""팔로우 사슬 라이브 스모크 — WS 선언 → 적재 → 인덱스 → personal 타임라인 (실 인프라).

사슬: WS /session follow.set → player.follow.changed 적재 → relay → LF_PLAYER →
redis-projector 인덱스 → 피드 포스트 팬아웃 → personal 타임라인(lf:tl).
철회(마지막 선언이 이긴다)와 무결성 검사(verify_timeline)까지 확인한다.
"""

import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path

import nats
import uvicorn
import websockets
from lf_dispatcher.relay import relay_once
from lf_dispatcher.streams import ensure_streams
from lf_eventstore import NewEvent, append, current_head
from lf_gateway.config import Config as GatewayConfig
from lf_gateway.main import create_app
from lf_projector.config import Config as ProjectorConfig
from lf_projector.redis_projector import RedisProjector
from lf_projector.timeline import TimelineStore
from lf_projector.timeline_verify import verify_timeline
from psycopg import AsyncConnection
from redis.asyncio import Redis

REPO = Path(__file__).resolve().parents[2]
PG_DSN = os.environ.get(
    "LF_SMOKE_PG_DSN", "postgresql://livingfeed:livingfeed@localhost:5433/livingfeed"
)
NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
REDIS_URL = os.environ.get("LF_SMOKE_REDIS_URL", "redis://localhost:6380/3")
RUN = os.urandom(3).hex()
ENV = f"smokefl{RUN}"
WORLD = "w_main"  # gateway 세션의 기본 세계 규약을 따른다
PLAYER = f"p_smoke_{RUN}"
ACTOR = "a_aria_kim"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def poll(label: str, fn, deadline_s: float = 20.0):
    start = time.monotonic()
    while time.monotonic() - start < deadline_s:
        result = await fn()
        if result:
            return result
        await asyncio.sleep(0.3)
    raise AssertionError(f"SMOKE FAIL — {label} 이(가) {deadline_s}s 안에 도달하지 않았다")


async def ws_follow(port: int, following: bool) -> None:
    async with websockets.connect(
        f"ws://127.0.0.1:{port}/session?player_id={PLAYER}"
    ) as ws:
        await ws.send(json.dumps({
            "type": "follow.set", "seq": 1,
            "payload": {"target_actor_id": ACTOR, "following": following},
        }))
        ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert ack["type"] == "ack", ack


async def main() -> None:
    conn = await AsyncConnection.connect(PG_DSN, autocommit=True)
    redis = Redis.from_url(REDIS_URL)
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    await ensure_streams(js)
    stop = asyncio.Event()

    gw_cfg = GatewayConfig(nats_url="unused(주입)", env=ENV, pg_dsn=PG_DSN)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(
        create_app(cfg=gw_cfg, nc=nc), host="127.0.0.1", port=port, log_level="warning"
    ))
    server_task = asyncio.create_task(server.serve())
    projector = RedisProjector(ProjectorConfig(
        nats_url=NATS_URL, opensearch_url="http://unused", env=ENV,
        database_url=PG_DSN, redis_url=REDIS_URL,
        redis_durable=f"smokefl-redis-{RUN}", fetch_timeout_s=1.0,
    ))
    projector_task = asyncio.create_task(projector.run(stop=stop))
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.1)
    store = TimelineStore(redis)
    try:
        # 1) WS 선언 → 적재 → relay → 인덱스
        await ws_follow(port, True)
        await relay_once(conn, js, ENV)
        await poll("팔로워 인덱스", lambda: _index_has(store))
        print(f"1) 팔로우 선언 → 인덱스: {PLAYER} 등록")

        # 2) 그 액터의 포스트가 personal 타임라인으로 팬아웃된다
        post = json.loads(
            (REPO / "packages" / "schemas" / "samples" / "feed.post.published.001.json")
            .read_text(encoding="utf-8")
        )
        head = await current_head(conn, WORLD, "feed", f"smk{RUN}")
        [stored] = await append(conn, "engine.feed", [NewEvent(
            world_id=WORLD, stream="feed", stream_key=f"smk{RUN}",
            type="feed.post.published", tick=99, actor_id=ACTOR,
            payload=post["payload"],
        )], expected_head=head)
        await relay_once(conn, js, ENV)
        timeline = await poll("personal 타임라인", lambda: _timeline_has(redis, store))
        print(f"2) 팬아웃: 타임라인 {len(timeline)}건 — 팔로우한 사람의 소식이 내 피드다")

        # 3) 무결성 검사가 이 상태를 무결로 본다 (관계 stand-in 없는 순수 명시 경로)
        report = await verify_timeline(conn, redis, world_id=WORLD)
        target = report["worlds"][WORLD]
        assert PLAYER not in json.dumps(target.get("mismatched", {})), target
        print("3) verify_timeline: 이 플레이어의 인덱스가 원천과 일치")

        # 4) 철회 — 마지막 선언이 이긴다
        await ws_follow(port, False)
        await relay_once(conn, js, ENV)
        await poll("철회 반영", lambda: _index_empty(store))
        print("4) 철회 → 인덱스에서 빠졌다 (거부 마커가 되살림을 막는다)")
        print("SMOKE: OK — 팔로우 선언→인덱스→팬아웃→철회 사슬이 닫혔다")
    finally:
        stop.set()
        server.should_exit = True
        await asyncio.gather(server_task, projector_task, return_exceptions=True)
        await nc.drain()
        await redis.aclose()
        await conn.close()


async def _index_has(store: TimelineStore):
    return PLAYER in await store.followers(WORLD, ACTOR)


async def _index_empty(store: TimelineStore):
    return PLAYER not in await store.followers(WORLD, ACTOR)


async def _timeline_has(redis: Redis, store: TimelineStore):
    return await redis.zrange(store.timeline_key(WORLD, PLAYER), 0, -1)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
