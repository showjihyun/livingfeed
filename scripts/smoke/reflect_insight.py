"""LLM reflection 라이브 스모크 — 실 Ollama(qwen3:8b)로 통찰 품질·스키마 준수 확인."""

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import nats
from lf_actor.client import AiRuntimeClient
from lf_actor.context import WorldContext, build
from lf_actor.persona import load_persona
from lf_actor.reflection import insight_schema, insight_to_belief
from lf_ai_runtime.config import Config
from lf_ai_runtime.service import serve

REPO = Path(__file__).resolve().parents[2]
NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
ENV = "reflectsmoke"


async def main() -> None:
    nc = await nats.connect(NATS_URL)
    stop = asyncio.Event()
    service = asyncio.create_task(
        serve(Config(nats_url=NATS_URL, env=ENV, provider="local"), stop=stop)
    )
    await asyncio.sleep(1.0)
    try:
        aria = load_persona(REPO / "agents" / "personas" / "aria-kim.yaml")
        known = ["a_junho_park", "p_observer_0417"]
        working = [
            "아는 사람들: 박준호(a_junho_park), 관찰자(p_observer_0417)",
            'tick 28: 나는 플레이어 p_observer_0417에게 답했다 — '
            '"고마워요, 그 말 오래 남을 것 같아요"',
            'tick 27: 플레이어 p_observer_0417의 DM: "기사 잘 봤어요. 위험해도 계속 써줘요."',
            "tick 25: 나는 work — 밤을 새워 제보 자료를 검증했다",
            "tick 20: 나는 work — 편집장이 기사를 또 미뤘다, 이유를 말하지 않는다",
            "tick 15: 나는 work — 제보자가 겁을 먹고 연락을 끊었다",
        ]
        world = WorldContext(
            world_id="w_smoke", tick=30, world_time=datetime(2026, 3, 2, tzinfo=UTC)
        )
        bundle = build(aria, working, world, purpose="reflect")
        client = AiRuntimeClient(nc, ENV, timeout_s=120)
        output = await client.reflect(
            bundle, insight_schema(known), actor_id=aria.id, tick=30
        )
        print("RAW OUTPUT:", output)
        belief = insight_to_belief(output or {}, set(known))
        print("BELIEF:", belief)
        if belief is None:
            print("SMOKE: FAIL — 통찰이 하드룰을 통과하지 못했다")
            sys.exit(1)
        print(
            f"SMOKE: OK — kind={belief.kind} conf={belief.confidence} "
            f"about={belief.about_id}\n  statement: {belief.statement}"
        )
    finally:
        stop.set()
        await service
        await nc.drain()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
