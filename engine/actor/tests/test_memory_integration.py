"""기억 응고 통합 — 개입이 에피소드로 접혀 이벤트가 된다 (ADR-008).

PG+Redis(+NATS) 필요 (없으면 skip — conftest 참고).
"""

from datetime import UTC, datetime

from lf_actor.client import AiRuntimeClient
from lf_actor.emotion import EmotionAdapter
from lf_actor.mailbox import Mailbox
from lf_actor.memory import WorkingMemory
from lf_actor.persona import load_persona
from lf_actor.phases import ActorPhases
from lf_actor.relationship import RelationshipAdapter
from lf_eventstore import read_stream
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick

from .conftest import PERSONAS_DIR
from .test_mailbox import player_envelope
from .test_phases import ai_service, nc  # noqa: F401 — 픽스처 재사용

WORLD = "w_test"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))


async def test_dm_tick_consolidates_memorable_episode(conn, redis, nc, ai_service):  # noqa: F811
    mailbox = Mailbox(redis)
    dm = player_envelope("player.dm.sent", {"text": "기사 응원해요. 진실은 힘이 세요."})
    await mailbox.push(WORLD, "a_aria_kim", dm)

    phases = ActorPhases(
        [load_persona(PERSONAS_DIR / "aria-kim.yaml")],
        ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis),
        mailbox=mailbox,
        emotion=EmotionAdapter(redis),
        relationship=RelationshipAdapter(redis),
        # semantic=None — Qdrant 없이도 응고 이벤트(원본)는 남는다
    )
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", "a_aria_kim")]
    [memory] = [e for e in events if e["type"] == "actor.memory.consolidated"]

    payload = memory["payload"]
    assert "응원해요" in payload["summary"]  # 개입이 요약에 남았다
    assert "나는 답했다" in payload["summary"]  # 자기 응답도
    assert dm["event_id"] in payload["source_event_ids"]  # 감사 추적 (규칙 4)
    assert memory["causation_id"] == dm["event_id"]
    # 감정(gratitude 강함) + 관계(first_met+변화) → Semantic 게이트(0.4) 이상
    assert payload["importance"] >= 0.4
    assert payload["factors"]["emotion"] > 0.5
    assert payload["factors"]["relationship"] > 0.5


async def test_quiet_actor_leaves_only_routine_memory(conn, redis, nc, ai_service):  # noqa: F811
    phases = ActorPhases(
        [load_persona(PERSONAS_DIR / "aria-kim.yaml")],
        ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis),
        mailbox=Mailbox(redis),
        emotion=EmotionAdapter(redis),
        relationship=RelationshipAdapter(redis),
    )
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", "a_aria_kim")]
    memories = [e for e in events if e["type"] == "actor.memory.consolidated"]
    if memories:  # 규칙 행동에 대상이 없으면 응고 자체가 없을 수도 있다
        assert all(m["payload"]["importance"] < 0.4 for m in memories)  # 일상은 낮다