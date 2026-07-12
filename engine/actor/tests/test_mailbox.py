"""메일박스 상호작용 경로 검증 — 개입 지각 → 응답 적재 (ADR-012).

PG+Redis(+NATS) 필요 (없으면 skip — conftest 참고).
rule 프로바이더는 converse를 지원하지 않으므로 답장은 규칙 폴백 경로다 —
'세계가 반드시 응답한다'는 보증을 검증한다.
"""

from datetime import UTC, datetime

from lf_actor.client import AiRuntimeClient
from lf_actor.mailbox import Mailbox
from lf_actor.memory import WorkingMemory
from lf_actor.persona import load_persona
from lf_actor.phases import ActorPhases
from lf_eventstore import new_ulid, read_stream
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick

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


def make_phases(nc, redis, env: str, mailbox: Mailbox) -> ActorPhases:  # noqa: F811
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    return ActorPhases(
        [aria],
        ai=AiRuntimeClient(nc, env, timeout_s=5),
        memory=WorkingMemory(redis),
        mailbox=mailbox,
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
    # 응답이 행동보다 앞선다 (상호작용 우선, ADR-012 규칙 2)
    assert types == ["actor.message.sent", "actor.action.performed"]

    reply = events[0]
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


async def test_reaction_is_perceived_but_not_replied(conn, redis, nc, ai_service):  # noqa: F811
    mailbox = Mailbox(redis)
    like = player_envelope(
        "player.reaction.added", {"post_id": new_ulid(), "kind": "like"}
    )
    await mailbox.push(WORLD, "a_aria_kim", like)

    phases = make_phases(nc, redis, ai_service, mailbox)
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", "a_aria_kim")]
    assert [e["type"] for e in events] == ["actor.action.performed"]  # 응답 의무 없음

    recent = await WorkingMemory(redis).recent(WORLD, "a_aria_kim")
    assert any("좋아요" in m for m in recent)  # 그러나 지각은 된다 (감정 입력, ADR-015)
