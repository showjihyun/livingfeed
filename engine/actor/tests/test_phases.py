"""ActorPhases 통합 검증 — 실제 PG+Redis(+NATS의 AI Runtime) 대상.

아리아가 tick 파이프라인을 타고 actor.action.performed를 남기는지 확인한다.
"""

import asyncio
import os
from datetime import UTC, datetime

import nats
import pytest
from lf_actor.client import AiRuntimeClient
from lf_actor.emotion import EmotionAdapter
from lf_actor.ledger import DecayLedger
from lf_actor.mailbox import Mailbox
from lf_actor.memory import WorkingMemory
from lf_actor.persona import load_persona
from lf_actor.phases import ActorPhases
from lf_actor.relationship import RelationshipAdapter
from lf_emotion import EmotionState
from lf_emotion import decay as emotion_decay
from lf_emotion.model import EmotionInstance, Pad
from lf_eventstore import new_ulid, read_stream
from lf_relationship import RelationshipState
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick
from lf_tick.lod import COLD_INTERVAL, ActorLod, Tier, phase_offset

from .conftest import NATS_URL, PERSONAS_DIR

WORLD = "w_test"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))


def actions_of(stored: list) -> list:
    """액터 스트림에서 확정 행동만 — 결정 기록(actor.decision.made)은 뺀다.

    ADR-021 §2 이후 같은 스트림에 "무엇을 보고 그렇게 했는가"가 함께 쌓인다.
    행동을 세는 단언이 그 기록까지 세면, 늘어난 관측이 곧 실패로 보인다.
    """
    return [s for s in stored if s.envelope["type"] == "actor.action.performed"]


@pytest.fixture
async def nc():
    if NATS_URL is None:
        pytest.skip("LF_TEST_NATS_URL 미설정 — 전용 테스트 NATS(4223)를 명시하라")
    client = await asyncio.wait_for(nats.connect(NATS_URL, connect_timeout=3), timeout=5)
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
async def ai_service(nc):
    """rule 프로바이더 AI Runtime을 in-process로 — 테스트별 고유 env로 격리."""
    from lf_ai_runtime.budget import BudgetGuard, MemoryStore
    from lf_ai_runtime.config import Config
    from lf_ai_runtime.service import serve

    env = f"t{os.urandom(4).hex()}"
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(
            Config(nats_url=NATS_URL, env=env, provider="rule"),
            stop=stop,
            # 예산 카운터를 프로세스 안에만 둔다 — 주입하지 않으면 REDIS_URL(기본
            # localhost:6379/0)에 붙어 상주 세계의 카운터에 섞어 쓴다 (격리 규약)
            guard=BudgetGuard(env, MemoryStore()),
        )
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

    [action] = actions_of(await read_stream(conn, WORLD, "actor", "a_aria_kim"))
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
    # 결정 기록 1 + 행동 1 — 결정하는 액터는 tick당 이벤트를 둘 낸다 (ADR-021 §2).
    # 이벤트 발생률이 이만큼 늘어난다는 사실 자체가 ADR-020 예산의 입력이다.
    assert completed["payload"]["events_emitted"] == 2

    # 자기 행동이 Working Memory로 유입됐다 (ADR-008)
    memory = WorkingMemory(redis)
    recent = await memory.recent(WORLD, "a_aria_kim")
    assert recent and "tick 0" in recent[0]


async def test_fallback_when_ai_runtime_is_down(conn, redis, nc):
    """AI Runtime 무응답 → 규칙 폴백으로 tick은 계속 흐른다 (ADR-012)."""
    phases = make_phases(nc, redis, env="nosuchenv")
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)
    assert head == 2

    [action] = actions_of(await read_stream(conn, WORLD, "actor", "a_aria_kim"))
    payload = action.envelope["payload"]
    assert payload["params"].get("fallback") is True
    assert payload["decision_trace"]["tier"] == "cold_rule"
    assert payload["action_kind"] == "work"  # 아리아의 최강 욕구(achievement) 기반


async def test_working_memory_feeds_next_tick_context(conn, redis, nc, ai_service):
    phases = make_phases(nc, redis, ai_service)
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)
    await run_tick(conn, phases, CLOCK, WORLD, tick=1, head=head)

    actions = actions_of(await read_stream(conn, WORLD, "actor", "a_aria_kim"))
    assert [a.envelope["tick"] for a in actions] == [0, 1]

    memory = WorkingMemory(redis)
    recent = await memory.recent(WORLD, "a_aria_kim")
    assert len(recent) == 2
    assert "tick 1" in recent[0]  # 최신 우선


async def test_idle_actor_demotes_hot_to_warm(conn, redis, nc, ai_service):
    """관심이 끊긴 액터는 히스테리시스를 지나면 Hot→Warm으로 강등된다 (ADR-011).

    강등이 곧 비용 정책 — 유휴 액터가 매 tick LLM을 부르지 않게 한다.
    """
    phases = make_phases(nc, redis, ai_service)
    head = 0
    for tick in range(0, 11):  # HOT_DEMOTION_GRACE(10) 유휴 tick
        head = await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=head)
    assert phases._lods["a_aria_kim"].tier is Tier.WARM  # 강등됨


async def test_cold_actor_uses_routine_action_without_llm(conn, redis, nc, ai_service):
    """Cold 티어는 통계 일괄 처리 — due일 때 LLM 없이 일과 행동만 (ADR-012).

    일과는 잠든 기간의 생활 요약이다 — 순간의 머뭇거림(fallback)이 아니라
    기간의 결이 담긴 서술로 남는다 ('일과 이벤트 생성').
    """
    phases = make_phases(nc, redis, ai_service)
    phases._lods["a_aria_kim"] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    due_tick = phase_offset("a_aria_kim", COLD_INTERVAL)  # 이 tick에 cold가 due
    await run_tick(conn, phases, CLOCK, WORLD, tick=due_tick, head=0)

    [action] = actions_of(await read_stream(conn, WORLD, "actor", "a_aria_kim"))
    payload = action.envelope["payload"]
    assert payload["params"].get("fallback") is True       # 규칙 경로 (LLM 미호출)
    assert payload["params"].get("routine") is True        # 일과 행동이다
    assert payload["decision_trace"]["tier"] == "cold_rule"
    assert payload["action_kind"] == "work"  # achievement 기조의 일과
    events = await read_stream(conn, WORLD, "system", "tick")
    assert events[-1].envelope["payload"]["actors_decided"]["cold"] == 1


async def test_cold_actor_decay_batches_at_due_tick(conn, redis, nc, ai_service):
    """Cold 액터는 매 tick 감쇠하지 않는다 — due tick에 경과분을 한 번에 (ADR-012
    §Cold 티어 처리). 결과는 엔진의 n-tick 감쇠 의미론과 정확히 일치한다."""
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    adapter = EmotionAdapter(redis)
    seeded = EmotionState(
        mood=Pad(pleasure=0.5, arousal=0.2, dominance=0.1),
        emotions=(
            EmotionInstance(type="joy", intensity=0.8, target_id=None, source_event="e1"),
        ),
    )
    await adapter.save(WORLD, "a_aria_kim", seeded)

    phases = ActorPhases(
        [aria], ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis), emotion=adapter,
    )
    due = phase_offset("a_aria_kim", COLD_INTERVAL)
    start = due + COLD_INTERVAL - 2  # due 2 tick 전부터 잠들어 있다
    phases._lods["a_aria_kim"] = ActorLod(tier=Tier.COLD, last_interest_tick=start)

    # 비-due tick 2번 — 상태에 손도 대지 않는다 (Redis 왕복 없음이 곧 비용 정책)
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=start, head=0)
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=start + 1, head=head)
    assert await adapter.load(WORLD, "a_aria_kim") == seeded

    # due tick — 밀린 3 tick(start..start+2)을 한 번에 배치 적용
    await run_tick(conn, phases, CLOCK, WORLD, tick=start + 2, head=head)
    expected = EmotionState.from_json(emotion_decay(seeded, aria.big_five, ticks=3).to_json())
    assert await adapter.load(WORLD, "a_aria_kim") == expected


async def test_cold_wakeup_catches_up_decay_before_appraise(conn, redis, nc, ai_service):
    """잠들었던 액터가 DM으로 깨어나면 밀린 감쇠를 지각 전에 정산한다 (ADR-012).

    정산이 없으면 오래된 감정이 갓 생긴 것처럼 강하게 남아 평가·결정을 오염시킨다.
    """
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    adapter = EmotionAdapter(redis)
    seeded = EmotionState(
        mood=Pad(pleasure=0.5, arousal=0.2, dominance=0.1),
        emotions=(
            EmotionInstance(type="joy", intensity=0.8, target_id=None, source_event="e1"),
        ),
    )
    await adapter.save(WORLD, "a_aria_kim", seeded)

    mailbox = Mailbox(redis)
    phases = ActorPhases(
        [aria], ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis), mailbox=mailbox, emotion=adapter,
    )
    phases._lods["a_aria_kim"] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    phases._last_decay["a_aria_kim"] = 0  # tick 0까지 정산된 채 잠들었다

    event_id = new_ulid()
    await mailbox.push(WORLD, "a_aria_kim", {
        "event_id": event_id, "stream": "player", "type": "player.dm.sent",
        "schema_version": 1, "world_id": WORLD, "actor_id": None, "tick": 10,
        "occurred_at": "2026-03-01T00:40:00Z", "causation_id": None,
        "correlation_id": event_id,
        "payload": {"player_id": "p_observer_0417", "target_actor_id": "a_aria_kim",
                    "text": "오랜만이에요"},
    })
    await run_tick(conn, phases, CLOCK, WORLD, tick=10, head=0)

    # joy = 정산 9 tick(1..9) 후 CONSOLIDATE가 이번 tick 몫 1을 마저 — 총 10 tick 감쇠.
    # 정산이 없었다면 1 tick 감쇠만 남아 눈에 띄게 강하다.
    state = await adapter.load(WORLD, "a_aria_kim")
    [joy] = [e for e in state.emotions if e.type == "joy"]
    caught_up = emotion_decay(seeded, aria.big_five, ticks=9)
    expected = emotion_decay(caught_up, aria.big_five, ticks=1)
    [expected_joy] = [e for e in expected.emotions if e.type == "joy"]
    assert joy.intensity == round(expected_joy.intensity, 4)
    assert any(e.type == "gratitude" for e in state.emotions)  # 지각은 정산 위에서
    assert phases._last_decay["a_aria_kim"] == 10  # 장부가 이 tick까지 정산됐다


async def test_decay_ledger_roundtrip(redis):
    ledger = DecayLedger(redis)
    assert await ledger.load_all(WORLD) == {}
    await ledger.mark(WORLD, {"a_aria_kim": 42, "a_minji_kim": 7})
    assert await ledger.load_all(WORLD) == {"a_aria_kim": 42, "a_minji_kim": 7}
    await ledger.mark(WORLD, {"a_aria_kim": 50})  # 부분 갱신 — 남은 항목은 유지
    assert await ledger.load_all(WORLD) == {"a_aria_kim": 50, "a_minji_kim": 7}


async def test_decay_ledger_survives_worker_restart(conn, redis, nc, ai_service):
    """감쇠 장부가 Redis에 살아 워커 재시작에도 Cold 경과가 이어진다 (ADR-012).

    영속 없이는 재시작한 워커가 경과를 몰라 밀린 감쇠가 조용히 사라진다
    (due tick에 1 tick 몫만 적용) — 장부가 있으면 전체 경과가 정산된다.
    """
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    adapter = EmotionAdapter(redis)
    seeded = EmotionState(
        mood=Pad(pleasure=0.5, arousal=0.2, dominance=0.1),
        emotions=(
            EmotionInstance(type="joy", intensity=0.8, target_id=None, source_event="e1"),
        ),
    )
    await adapter.save(WORLD, "a_aria_kim", seeded)
    due = phase_offset("a_aria_kim", COLD_INTERVAL)
    start = due + COLD_INTERVAL - 2

    first = ActorPhases(
        [aria], ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis), emotion=adapter, decay_ledger=DecayLedger(redis),
    )
    first._lods["a_aria_kim"] = ActorLod(tier=Tier.COLD, last_interest_tick=start)
    head = await run_tick(conn, first, CLOCK, WORLD, tick=start, head=0)

    # 워커 재시작 — 새 인스턴스의 in-memory 장부는 비어 있지만 Redis에서 수화한다
    second = ActorPhases(
        [aria], ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis), emotion=adapter, decay_ledger=DecayLedger(redis),
    )
    second._lods["a_aria_kim"] = ActorLod(tier=Tier.COLD, last_interest_tick=start)
    head = await run_tick(conn, second, CLOCK, WORLD, tick=start + 1, head=head)
    assert await adapter.load(WORLD, "a_aria_kim") == seeded  # 여전히 잠들어 있다

    await run_tick(conn, second, CLOCK, WORLD, tick=start + 2, head=head)  # due
    # 재시작을 가로질러 경과 3 tick이 온전히 정산됐다
    expected = EmotionState.from_json(emotion_decay(seeded, aria.big_five, ticks=3).to_json())
    assert await adapter.load(WORLD, "a_aria_kim") == expected


async def test_cold_wakeup_relationship_drift_joins_consolidate(conn, redis, nc, ai_service):
    """재기상 정산의 관계 감쇠 발행분은 버려지지 않고 CONSOLIDATE 적재에 합류한다
    (조용한 유실 금지). 오래 식은 관계가 깨어나는 tick에 세계에 기록된다."""
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    rel = RelationshipAdapter(redis)
    base = RelationshipState()
    seeded = RelationshipState(
        dimensions={**base.dimensions, "intimacy": 0.9},
        stage="friend", salience=0.8, pending=base.pending,
    )
    await rel.save(WORLD, "a_aria_kim", "p_observer_0417", seeded)
    await rel._register_edge(WORLD, "a_aria_kim", "p_observer_0417")

    mailbox = Mailbox(redis)
    phases = ActorPhases(
        [aria], ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis), mailbox=mailbox, relationship=rel,
    )
    phases._lods["a_aria_kim"] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    phases._last_decay["a_aria_kim"] = 0

    # 300 tick 잠들었다 깨어난다 — intimacy 드리프트 299×0.0003 ≈ 0.09 ≥ 발행 임계 0.08
    event_id = new_ulid()
    await mailbox.push(WORLD, "a_aria_kim", {
        "event_id": event_id, "stream": "player", "type": "player.dm.sent",
        "schema_version": 1, "world_id": WORLD, "actor_id": None, "tick": 300,
        "occurred_at": "2026-03-02T00:00:00Z", "causation_id": None,
        "correlation_id": event_id,
        "payload": {"player_id": "p_observer_0417", "target_actor_id": "a_aria_kim",
                    "text": "잘 지냈어요?"},
    })
    await run_tick(conn, phases, CLOCK, WORLD, tick=300, head=0)

    events = [
        s.envelope
        for s in await read_stream(conn, WORLD, "relationship", "a_aria_kim|p_observer_0417")
    ]
    drifts = [e for e in events if "시간 감쇠" in e["payload"].get("reason", "")]
    assert drifts  # 식은 관계가 세계에 기록됐다 — 정산분이 유실되지 않았다
    assert drifts[0]["payload"]["deltas"]["intimacy"] < -0.08
