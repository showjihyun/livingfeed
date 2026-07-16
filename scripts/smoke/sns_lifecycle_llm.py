"""SNS 생활 라이브 스모크 — 실 LLM(local 프로바이더)으로 신규 캐릭터의 생활 관찰.

test_persona_sns_lifecycle.py(규칙 폴백 보증)의 LLM 확장: 같은 사이클을
qwen3가 산다 — 행동 intent·헤드라인·답장이 템플릿이 아니라 이 인물의
문장인지 눈으로 확인한다 (표현 품질은 정성 평가 — 출력을 읽어라).
"""

import asyncio
import os
import sys
from datetime import UTC, datetime

import nats
from lf_actor.client import AiRuntimeClient
from lf_actor.emotion import EmotionAdapter
from lf_actor.goal import GoalAdapter
from lf_actor.mailbox import Mailbox
from lf_actor.memory import WorkingMemory
from lf_actor.persona import Persona
from lf_actor.phases import ActorPhases
from lf_actor.relationship import RelationshipAdapter
from lf_ai_runtime.config import Config as AiConfig
from lf_ai_runtime.service import serve
from lf_eventstore import new_ulid, read_stream
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick
from psycopg import AsyncConnection
from redis.asyncio import Redis

PG_DSN = os.environ.get(
    "LF_SMOKE_PG_DSN", "postgresql://livingfeed:livingfeed@localhost:5433/livingfeed"
)
NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
REDIS_URL = os.environ.get("LF_SMOKE_REDIS_URL", "redis://localhost:6380/3")
RUN = os.urandom(3).hex()
ENV = f"smokelife{RUN}"
WORLD = f"w_smokelife{RUN}"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))


def make_newcomer() -> Persona:
    """즉석 조립 신규 캐릭터 — 파일·특정 인물 하드코딩 없음 (테스트 계획과 동일 스펙)."""
    return Persona(
        id="a_probe_newcomer",
        name="새내기",
        archetype="probe_newcomer",
        identity_core="방금 이 세계에 들어온 신입 — 자기 자리를 만들려 애쓰는 중이다.",
        big_five={"openness": 0.6, "conscientiousness": 0.7, "extraversion": 0.5,
                  "agreeableness": 0.6, "neuroticism": 0.5},
        needs_bias={"achievement": 0.8, "belonging": 0.6, "security": 0.4},
        goals=({"id": "g_first_step", "description": "첫 결과물을 세상에 내놓기",
                "priority": 0.8, "need": "achievement"},),
    )


def dm_envelope(text: str) -> dict:
    event_id = new_ulid()
    return {
        "event_id": event_id, "stream": "player", "type": "player.dm.sent",
        "schema_version": 1, "world_id": WORLD, "actor_id": None, "tick": 0,
        "occurred_at": "2026-03-01T00:00:00Z", "causation_id": None,
        "correlation_id": event_id,
        "payload": {"player_id": "p_watcher", "target_actor_id": "a_probe_newcomer",
                    "text": "처음 보는 얼굴이네요. 어떤 일을 하는 분인가요?"},
    }


async def main() -> None:
    conn = await AsyncConnection.connect(PG_DSN, autocommit=True)
    redis = Redis.from_url(REDIS_URL)
    stop = asyncio.Event()
    ai_task = asyncio.create_task(
        serve(AiConfig(nats_url=NATS_URL, env=ENV, provider="local"), stop=stop)
    )
    nc = await nats.connect(NATS_URL)
    await asyncio.sleep(1.0)
    try:
        persona = make_newcomer()
        mailbox = Mailbox(redis)
        phases = ActorPhases(
            [persona],
            ai=AiRuntimeClient(nc, ENV, timeout_s=120),  # qwen3 thinking 지연 여유
            memory=WorkingMemory(redis),
            mailbox=mailbox,
            emotion=EmotionAdapter(redis),
            relationship=RelationshipAdapter(redis),
            goal=GoalAdapter(redis),
            identity_redis=redis,
        )
        await mailbox.push(WORLD, persona.id, dm_envelope("환영 인사"))

        head = 0
        for tick in range(3):
            head = await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=head)
            print(f"— tick {tick} 완료")

        events = [s.envelope for s in await read_stream(conn, WORLD, "actor", persona.id)]
        actions = [e for e in events if e["type"] == "actor.action.performed"]
        replies = [e for e in events if e["type"] == "actor.message.sent"]
        assert actions and replies, "행동 또는 답장이 없다 — 생활이 멈췄다"

        llm_actions = [a for a in actions if not a["payload"]["params"].get("fallback")]
        print(f"\n행동 {len(actions)}건 (LLM {len(llm_actions)} / 폴백 "
              f"{len(actions) - len(llm_actions)}):")
        for act in actions:
            marker = "llm" if not act["payload"]["params"].get("fallback") else "rule"
            headline = act["payload"].get("headline") or "-"
            print(f"  [{marker}] {act['payload']['intent']}  (headline: {headline})")
        print(f"\n답장: {replies[0]['payload']['text']!r}")
        print(
            "\nSMOKE: OK — 신규 캐릭터가 LLM으로 SNS 생활을 살았다"
            if llm_actions else
            "\nSMOKE: WEAK — 전부 규칙 폴백이었다 (모델 지연·오류 확인 필요)"
        )
    finally:
        stop.set()
        await ai_task
        await nc.drain()
        await redis.aclose()
        await conn.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
