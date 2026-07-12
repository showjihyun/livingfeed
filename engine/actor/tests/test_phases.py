"""ActorPhases 통합 검증 — 실제 PG+Redis(+NATS의 AI Runtime) 대상.

아리아가 tick 파이프라인을 타고 actor.action.performed를 남기는지 확인한다.
"""

import asyncio
import os
from datetime import UTC, datetime

import nats
import pytest
from lf_actor.client import AiRuntimeClient
from lf_actor.memory import WorkingMemory
from lf_actor.persona import load_persona
from lf_actor.phases import ActorPhases
from lf_eventstore import read_stream
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick

from .conftest import NATS_URL, PERSONAS_DIR

WORLD = "w_test"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))


@pytest.fixture
async def nc():
    try:
        client = await asyncio.wait_for(nats.connect(NATS_URL, connect_timeout=3), timeout=5)
    except Exception:
        if "LF_TEST_NATS_URL" in os.environ:
            raise
        pytest.skip(f"NATS 미가용 ({NATS_URL}) — infra/compose에서 nats를 켜라")
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
async def ai_service(nc):
    """rule 프로바이더 AI Runtime을 in-process로 — 테스트별 고유 env로 격리."""
    from lf_ai_runtime.config import Config
    from lf_ai_runtime.service import serve

    env = f"t{os.urandom(4).hex()}"
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(Config(nats_url=NATS_URL, env=env, provider="rule"), stop=stop)
    )
    await asyncio.sleep(0.2)
    try:
        yield env
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)


def make_phases(nc, redis, env: str) -> ActorPhases:
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    return ActorPhases(
        [aria], ai=AiRuntimeClient(nc, env, timeout_s=5), memory=WorkingMemory(redis)
    )


async def test_aria_acts_through_full_tick(conn, redis, nc, ai_service):
    phases = make_phases(nc, redis, ai_service)
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)
    assert head == 2  # started + completed

    [action] = await read_stream(conn, WORLD, "actor", "a_aria_kim")
    env = action.envelope
    assert env["type"] == "actor.action.performed"
    assert env["actor_id"] == "a_aria_kim"
    assert env["tick"] == 0
    assert env["payload"]["action_kind"]
    # AI Runtime(rule 프로바이더) 경로로 결정됨 — 폴백 표식 없음
    assert "fallback" not in env["payload"]["params"]

    # completed의 actors_decided에 hot 1 반영 (ADR-011)
    events = await read_stream(conn, WORLD, "system", "tick")
    completed = events[-1].envelope
    assert completed["payload"]["actors_decided"] == {"hot": 1, "warm": 0, "cold": 0}
    assert completed["payload"]["events_emitted"] == 1

    # 자기 행동이 Working Memory로 유입됐다 (ADR-008)
    memory = WorkingMemory(redis)
    recent = await memory.recent(WORLD, "a_aria_kim")
    assert recent and "tick 0" in recent[0]


async def test_fallback_when_ai_runtime_is_down(conn, redis, nc):
    """AI Runtime 무응답 → 규칙 폴백으로 tick은 계속 흐른다 (ADR-012)."""
    phases = make_phases(nc, redis, env="nosuchenv")
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)
    assert head == 2

    [action] = await read_stream(conn, WORLD, "actor", "a_aria_kim")
    payload = action.envelope["payload"]
    assert payload["params"].get("fallback") is True
    assert payload["decision_trace"]["tier"] == "cold_rule"
    assert payload["action_kind"] == "work"  # 아리아의 최강 욕구(achievement) 기반


async def test_working_memory_feeds_next_tick_context(conn, redis, nc, ai_service):
    phases = make_phases(nc, redis, ai_service)
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)
    await run_tick(conn, phases, CLOCK, WORLD, tick=1, head=head)

    actions = await read_stream(conn, WORLD, "actor", "a_aria_kim")
    assert [a.envelope["tick"] for a in actions] == [0, 1]

    memory = WorkingMemory(redis)
    recent = await memory.recent(WORLD, "a_aria_kim")
    assert len(recent) == 2
    assert "tick 1" in recent[0]  # 최신 우선
