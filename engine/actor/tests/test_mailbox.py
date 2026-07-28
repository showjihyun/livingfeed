"""메일박스 상호작용 경로 검증 — 개입 지각 → 응답 적재 (ADR-012).

PG+Redis(+NATS) 필요 (없으면 skip — conftest 참고).
rule 프로바이더는 converse를 지원하지 않으므로 답장은 규칙 폴백 경로다 —
'세계가 반드시 응답한다'는 보증을 검증한다.
"""

from datetime import UTC, datetime

from lf_actor.arc import Arc, ArcStore
from lf_actor.client import AiRuntimeClient
from lf_actor.mailbox import Mailbox
from lf_actor.memory import WorkingMemory
from lf_actor.persona import load_persona
from lf_actor.phases import ActorPhases
from lf_eventstore import new_ulid, read_stream
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick
from lf_tick.lod import ActorLod, Tier

from .conftest import PERSONAS_DIR
from .test_phases import ai_service, nc  # noqa: F401 — 픽스처 재사용

WORLD = "w_test"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))


def player_envelope(event_type: str, payload_extra: dict) -> dict:
    event_id = new_ulid()
    return {
        "event_id": event_id,
        "stream": "player",
        "type": event_type,
        "schema_version": 1,
        "world_id": WORLD,
        "actor_id": None,
        "tick": 0,
        "occurred_at": "2026-03-01T00:00:00Z",
        "causation_id": None,
        "correlation_id": event_id,
        "payload": {"player_id": "p_observer_0417", "target_actor_id": "a_aria_kim",
                    **payload_extra},
    }


def make_phases(
    nc, redis, env: str, mailbox: Mailbox, *, arc: ArcStore | None = None  # noqa: F811
) -> ActorPhases:
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    return ActorPhases(
        [aria],
        ai=AiRuntimeClient(nc, env, timeout_s=5),
        memory=WorkingMemory(redis),
        mailbox=mailbox,
        arc=arc,
    )


async def test_mailbox_push_drain_roundtrip(redis):
    mailbox = Mailbox(redis)
    first = player_envelope("player.dm.sent", {"text": "안녕하세요"})
    second = player_envelope("player.dm.sent", {"text": "요즘 어때요?"})
    await mailbox.push(WORLD, "a_aria_kim", first)
    await mailbox.push(WORLD, "a_aria_kim", second)

    drained = await mailbox.drain(WORLD, "a_aria_kim")
    assert [d["event_id"] for d in drained] == [first["event_id"], second["event_id"]]
    assert await mailbox.drain(WORLD, "a_aria_kim") == []  # 비웠다


async def test_player_dm_gets_reply_through_tick(conn, redis, nc, ai_service):  # noqa: F811
    mailbox = Mailbox(redis)
    dm = player_envelope("player.dm.sent", {"text": "기획안 얘기 봤어요. 응원해요."})
    await mailbox.push(WORLD, "a_aria_kim", dm)

    phases = make_phases(nc, redis, ai_service, mailbox)
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", "a_aria_kim")]
    types = [e["type"] for e in events]
    # 응답이 행동보다 앞선다 (상호작용 우선, ADR-012 규칙 2) — 응고가 tick을 닫는다.
    # 앞의 결정 기록 둘은 답장과 행동의 입력이다 (DECIDE(3) → RESOLVE(4), ADR-021 §2)
    assert types == [
        "actor.decision.made", "actor.decision.made",
        "actor.message.sent", "actor.action.performed", "actor.memory.consolidated",
    ]

    # 위치가 아니라 타입으로 집는다 — 결정 기록이 앞에 붙어도 계약은 그대로다
    [reply] = [e for e in events if e["type"] == "actor.message.sent"]
    assert reply["causation_id"] == dm["event_id"]
    assert reply["correlation_id"] == dm["correlation_id"]
    assert reply["payload"]["channel"] == "dm"
    assert reply["payload"]["target_player_id"] == "p_observer_0417"
    assert reply["payload"]["in_reply_to"] == dm["event_id"]
    assert reply["payload"]["text"]  # 규칙 폴백이라도 반드시 응답한다

    # 개입과 자기 응답이 Working Memory에 남았다 (ADR-008)
    recent = await WorkingMemory(redis).recent(WORLD, "a_aria_kim")
    assert any("플레이어 p_observer_0417의 DM" in m for m in recent)
    assert any("답했다" in m for m in recent)


async def test_player_dm_repromotes_demoted_actor_to_hot(conn, redis, nc, ai_service):  # noqa: F811
    """강등된 액터도 플레이어가 말을 걸면 즉시 Hot 복귀 (상호작용 우선, ADR-011/012)."""
    mailbox = Mailbox(redis)
    phases = make_phases(nc, redis, ai_service, mailbox)
    # 관심이 끊겨 Cold까지 내려간 상태로 시드
    phases._lods["a_aria_kim"] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    dm = player_envelope("player.dm.sent", {"text": "오랜만이에요"})
    await mailbox.push(WORLD, "a_aria_kim", dm)

    await run_tick(conn, phases, CLOCK, WORLD, tick=5, head=0)
    assert phases._lods["a_aria_kim"].tier is Tier.HOT  # DM이 관심 신호 → 재승격
    assert phases._lods["a_aria_kim"].last_interest_tick == 5


async def test_director_spotlight_promotes_lod_without_entering_memory(
    conn, redis, nc, ai_service  # noqa: F811
):
    """Director 승격 신호(promote_actor)는 LOD만 Hot으로 올린다 — 지각이 아니라
    제어라 액터가 '눈치채지' 않는다(작업 기억에 안 남는다, ADR-013)."""
    mailbox = Mailbox(redis)
    phases = make_phases(nc, redis, ai_service, mailbox)
    phases._lods["a_aria_kim"] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    spot = player_envelope("system.director.spotlighted", {"target_actor_id": "a_aria_kim"})
    await mailbox.push(WORLD, "a_aria_kim", spot)

    await run_tick(conn, phases, CLOCK, WORLD, tick=7, head=0)
    assert phases._lods["a_aria_kim"].tier is Tier.HOT  # 승격됨
    # 제어 신호 자체는 작업 기억에 남지 않는다 (지각한 것이 아니다)
    recent = await WorkingMemory(redis).recent(WORLD, "a_aria_kim")
    assert not any("spotlight" in m.lower() for m in recent)


async def test_director_arc_planned_stores_arc_without_entering_memory(
    conn, redis, nc, ai_service  # noqa: F811
):
    """Director의 인생 아크(plan_arc)는 ArcStore에만 남는다 — 지각이 아니라 제어라
    기억에 안 남고 LOD도 건드리지 않는다. 다음 decide부터 배경으로 스민다 (ADR-013)."""
    mailbox = Mailbox(redis)
    phases = make_phases(nc, redis, ai_service, mailbox, arc=ArcStore(redis))
    phases._lods["a_aria_kim"] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    plan = player_envelope(
        "system.director.arc_planned",
        {"stage": "newcomer", "intention": "이 도시에서 자기 자리를 만들기 시작한다"},
    )
    await mailbox.push(WORLD, "a_aria_kim", plan)

    await run_tick(conn, phases, CLOCK, WORLD, tick=7, head=0)
    # 아크는 저장됐다 — 다음 decide 컨텍스트의 최상위 프레임이 된다
    assert await ArcStore(redis).get(WORLD, "a_aria_kim") == Arc(
        stage="newcomer", intention="이 도시에서 자기 자리를 만들기 시작한다"
    )
    assert phases._lods["a_aria_kim"].tier is Tier.COLD  # 아크는 승격 신호가 아니다
    # 제어 신호 자체는 작업 기억에 남지 않는다 (지각한 것이 아니다)
    recent = await WorkingMemory(redis).recent(WORLD, "a_aria_kim")
    assert not any("아크" in m or "newcomer" in m for m in recent)


async def test_reaction_is_perceived_but_not_replied(conn, redis, nc, ai_service):  # noqa: F811
    mailbox = Mailbox(redis)
    like = player_envelope(
        "player.reaction.added", {"post_id": new_ulid(), "kind": "like"}
    )
    await mailbox.push(WORLD, "a_aria_kim", like)

    phases = make_phases(nc, redis, ai_service, mailbox)
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", "a_aria_kim")]
    # 응답 의무는 없지만 지각은 기억으로 응고된다 (ADR-008).
    # 답할 의무가 없으니 결정도 하나뿐이다 — 행동의 결정 (ADR-021 §2)
    assert [e["type"] for e in events] == [
        "actor.decision.made", "actor.action.performed", "actor.memory.consolidated",
    ]

    recent = await WorkingMemory(redis).recent(WORLD, "a_aria_kim")
    assert any("좋아요" in m for m in recent)  # 그러나 지각은 된다 (감정 입력, ADR-015)
