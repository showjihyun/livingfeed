"""narrate·boost 라이브 스모크 — 실 Ollama(qwen3)로 편집 조명·서사화 사슬 확인.

사슬 A (조명): feed_boosted 적재 → relay → composer 조명 → 경계 행동(work)이 승격.
사슬 B (서사): 고드라마 incident → composer가 narrate(qwen3) → body가 서사로.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import nats
from lf_ai_runtime.config import Config as AiConfig
from lf_ai_runtime.service import serve
from lf_dispatcher.relay import relay_once
from lf_dispatcher.streams import ensure_streams
from lf_eventstore import NewEvent, Provenance, append, current_head, read_stream
from lf_feed.compose import derive_post_id
from lf_feed.composer import FeedComposer
from lf_feed.config import Config as FeedConfig
from lf_feed.narrator import FeedNarrator
from psycopg import AsyncConnection

REPO = Path(__file__).resolve().parents[2]
PG_DSN = os.environ.get(
    "LF_SMOKE_PG_DSN", "postgresql://livingfeed:livingfeed@localhost:5433/livingfeed"
)
NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
RUN = os.urandom(3).hex()
ENV = f"smokenb{RUN}"
WORLD = f"w_smokenb{RUN}"

SAMPLES = REPO / "packages" / "schemas" / "samples"


def sample(name: str) -> dict:
    return json.loads((SAMPLES / f"{name}.001.json").read_text(encoding="utf-8"))


async def poll(label: str, fn, deadline_s: float = 90.0):
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

    ai_task = asyncio.create_task(
        serve(AiConfig(nats_url=NATS_URL, env=ENV, provider="local"), stop=stop)
    )
    composer = FeedComposer(
        FeedConfig(
            pg_dsn=PG_DSN, nats_url=NATS_URL, env=ENV,
            personas_dir=REPO / "agents" / "personas",
            durable=f"smokenb-composer-{RUN}", fetch_timeout_s=1.0,
        ),
        narrator=FeedNarrator(nc, ENV, timeout_s=90),  # qwen3 thinking 지연 여유
    )
    composer_task = asyncio.create_task(composer.run(stop=stop))
    await asyncio.sleep(1.0)
    try:
        # 사슬 A — 편집 조명: 경계의 work 행동이 조명으로 임계를 넘는다
        head = await current_head(conn, WORLD, "system", "boost")
        await append(conn, "engine.director", [NewEvent(
            world_id=WORLD, stream="system", stream_key="boost",
            type="system.director.feed_boosted", tick=40,
            provenance=Provenance.derived("smoke:narrate_boost"),
            payload={"target_actor_id": "a_aria_kim", "boost": 0.6, "until_tick": 200},
        )], expected_head=head)
        await relay_once(conn, js, ENV)
        await asyncio.sleep(1.5)  # 조명 소비 대기 (제어 신호 — 산출물이 없어 폴링 불가)

        action = sample("actor.action.performed")
        work_payload = {
            **action["payload"], "action_kind": "work", "target_actor_id": None,
        }
        work_payload.pop("headline", None)  # 템플릿 제목 경로 — headline은 nullable이 아니다
        action_head = await current_head(conn, WORLD, "actor", "a_aria_kim")
        [stored_action] = await append(conn, "engine.actor", [NewEvent(
            world_id=WORLD, stream="actor", stream_key="a_aria_kim",
            type="actor.action.performed", tick=50, actor_id="a_aria_kim",
            provenance=Provenance.derived("smoke:narrate_boost"),
            payload=work_payload,
        )], expected_head=action_head)
        await relay_once(conn, js, ENV)
        action_id = stored_action.envelope["event_id"]
        post_a = await poll(
            "조명 승격 피드",
            lambda: read_stream(conn, WORLD, "feed", derive_post_id(action_id)),
        )
        pa = post_a[0].envelope["payload"]
        assert pa["worthiness"] >= 0.35, pa["worthiness"]
        print(f"A) 조명 승격: worthiness={pa['worthiness']} (경계 행동이 임계를 넘었다)")

        # 사슬 B — 고드라마 incident의 본문이 qwen3 서사로
        incident = sample("world.incident.occurred")
        inc_head = await current_head(conn, WORLD, "world", "incidents")
        [stored_inc] = await append(conn, "engine.director", [NewEvent(
            world_id=WORLD, stream="world", stream_key="incidents",
            type="world.incident.occurred", tick=60,
            provenance=Provenance.derived("smoke:narrate_boost"),
            payload=incident["payload"],
        )], expected_head=inc_head)
        await relay_once(conn, js, ENV)
        inc_id = stored_inc.envelope["event_id"]
        post_b = await poll(
            "서사화 피드",
            lambda: read_stream(conn, WORLD, "feed", derive_post_id(inc_id)),
        )
        pb = post_b[0].envelope["payload"]
        print(f"B) narration_kind={pb['narration_kind']}")
        print(f"   원문: {incident['payload']['description']}")
        print(f"   본문: {pb['body']}")
        assert pb["narration_kind"] == "llm", "서사화가 일어나지 않았다"
        assert pb["body"] != incident["payload"]["description"]
        print("SMOKE: OK — 조명·서사화 사슬이 라이브로 닫혔다")
    finally:
        stop.set()
        await asyncio.gather(ai_task, composer_task, return_exceptions=True)
        await nc.drain()
        await conn.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
