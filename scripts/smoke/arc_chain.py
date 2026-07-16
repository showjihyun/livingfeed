"""E2E 스모크 — 아크 계획→relay→전이 피드→pg-projector→프로필 사슬 (실 PG+NATS).

사슬: append(arc_planned) → outbox relay → LF_SYS → FeedComposer(장 전환 승격)
     + PgProjector(read.actor_arcs) → ProfileReads(프로필 arc) → 시즌 회고.
"""

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import nats
from lf_director.retrospect import season_retrospective
from lf_dispatcher.relay import relay_once
from lf_dispatcher.streams import ensure_streams
from lf_eventstore import NewEvent, append, current_head, read_stream
from lf_feed.compose import derive_post_id
from lf_feed.composer import FeedComposer
from lf_feed.config import Config as FeedConfig
from lf_feed_api.reads import ProfileReads
from lf_projector.config import Config as ProjectorConfig
from lf_projector.pg_projector import PgProjector
from psycopg import AsyncConnection

REPO = Path(__file__).resolve().parents[2]
PG_DSN = os.environ.get(
    "LF_SMOKE_PG_DSN", "postgresql://livingfeed:livingfeed@localhost:5433/livingfeed"
)
NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
RUN = os.urandom(3).hex()
ENV = f"smokearc{RUN}"
WORLD = f"w_smokearc{RUN}"
ARC_TYPE = "system.director.arc_planned"


class OneConnPool:
    def __init__(self, conn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def connection(self):
        yield self._conn


async def append_arc(conn, tick: int, stage: str, intention: str) -> str:
    head = await current_head(conn, WORLD, "system", "arc")
    [stored] = await append(
        conn, "engine.director",
        [NewEvent(
            world_id=WORLD, stream="system", stream_key="arc", type=ARC_TYPE,
            tick=tick,
            payload={"target_actor_id": "a_minji_kim", "stage": stage,
                     "intention": intention},
        )],
        expected_head=head,
    )
    return stored.envelope["event_id"]


async def poll(label: str, fn, deadline_s: float = 25.0):
    start = time.monotonic()
    while time.monotonic() - start < deadline_s:
        result = await fn()
        if result:
            return result
        await asyncio.sleep(0.5)
    raise AssertionError(f"SMOKE FAIL — {label} 이(가) {deadline_s}s 안에 도달하지 않았다")


async def main() -> None:
    conn = await AsyncConnection.connect(PG_DSN, autocommit=True)
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    await ensure_streams(js)
    stop = asyncio.Event()

    composer = FeedComposer(FeedConfig(
        pg_dsn=PG_DSN, nats_url=NATS_URL, env=ENV,
        personas_dir=REPO / "agents" / "personas",
        durable=f"smokearc-composer-{RUN}", fetch_timeout_s=1.0,
    ))
    projector = PgProjector(ProjectorConfig(
        nats_url=NATS_URL, opensearch_url="http://unused", env=ENV,
        database_url=PG_DSN, pg_durable=f"smokearc-pg-{RUN}", fetch_timeout_s=1.0,
    ))
    workers = [
        asyncio.create_task(composer.run(stop=stop)),
        asyncio.create_task(projector.run(stop=stop)),
    ]
    try:
        # 1장: 첫 아크 (settling) — 이야기의 첫 장
        arc1 = await append_arc(
            conn, 360, "settling", "안정된 자리를 갈망하며 이직을 결단하려 한다"
        )
        await relay_once(conn, js, ENV)
        post1 = await poll(
            "첫 장 피드",
            lambda: read_stream(conn, WORLD, "feed", derive_post_id(arc1)),
        )
        title1 = post1[0].envelope["payload"]["title"]
        assert "첫 장이 열리다" in title1, title1
        print(f"1) 첫 아크 → 피드: {title1!r}")

        # 2장: 장 전환 (settling → prime)
        arc2 = await append_arc(conn, 720, "prime", "결단 이후 — 새 자리에서 증명한다")
        await relay_once(conn, js, ENV)
        post2 = await poll(
            "전이 피드",
            lambda: read_stream(conn, WORLD, "feed", derive_post_id(arc2)),
        )
        payload2 = post2[0].envelope["payload"]
        assert "인생의 장이 넘어가다" in payload2["title"], payload2["title"]
        assert "정착·방황기" in payload2["body"] and "전성기·침체기" in payload2["body"]
        print(f"2) 장 전환 → 피드: {payload2['title']!r} / tags={payload2['tags']}")

        # 3) read 모델: 최신 아크가 자리를 덮어썼다
        async def arc_row():
            rows = await (await conn.execute(
                "SELECT stage, intention FROM read.actor_arcs"
                " WHERE world_id = %s AND actor_id = 'a_minji_kim'", (WORLD,)
            )).fetchall()
            return rows if rows and rows[0][0] == "prime" else None
        [row] = await poll("read.actor_arcs(prime)", arc_row)
        print(f"3) read.actor_arcs: stage={row[0]!r} intention={row[1][:20]!r}…")

        # 4) 프로필 계약: feed-api reads가 arc를 싣는다
        profile = await ProfileReads(OneConnPool(conn)).actor_profile(
            WORLD, "a_minji_kim", episode_limit=5, episode_cursor=None
        )
        assert profile["arc"]["stage"] == "prime"
        print(f"4) 프로필 arc: {profile['arc']['stage']!r} — 인생의 장이 화면 계약까지 닿았다")

        # 5) 시즌 회고: day 1(tick 360~719)의 연출 요약
        report = await season_retrospective(conn, WORLD, day=1)
        assert report["arcs"] == {"planned": 1, "transitions": 1}
        print(
            f"5) 시즌 회고 day1: arcs={report['arcs']} "
            f"interventions={report['interventions']['total']}"
        )

        print("SMOKE: OK — 아크 계획→피드→read 모델→프로필→회고 사슬이 닫혔다")
    finally:
        stop.set()
        await asyncio.gather(*workers, return_exceptions=True)
        await nc.drain()
        await conn.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
