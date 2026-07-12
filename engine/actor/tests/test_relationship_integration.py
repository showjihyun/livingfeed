"""관계 응고 통합 — 개입·감정이 관계 엣지로 굳는다 (ADR-016).

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
PLAYER = "p_observer_0417"
EDGE = f"a_aria_kim|{PLAYER}"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))


def make_phases(nc, redis, env: str, mailbox: Mailbox) -> ActorPhases:  # noqa: F811
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    return ActorPhases(
        [aria],
        ai=AiRuntimeClient(nc, env, timeout_s=5),
        memory=WorkingMemory(redis),
        mailbox=mailbox,
        emotion=EmotionAdapter(redis),
        relationship=RelationshipAdapter(redis),
    )


async def test_dm_consolidates_into_relationship_edge(conn, redis, nc, ai_service):  # noqa: F811
    mailbox = Mailbox(redis)
    dm = player_envelope("player.dm.sent", {"text": "기사 응원해요. 진실은 힘이 세요."})
    await mailbox.push(WORLD, "a_aria_kim", dm)

    phases = make_phases(nc, redis, ai_service, mailbox)
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "relationship", EDGE)]
    types = [e["type"] for e in events]
    # 첫 접촉: first_met 마일스톤 → 상태 변화 (상호작용 효과 + 감정 응고)
    assert types[0] == "relationship.milestone.reached"
    assert "relationship.state.changed" in types

    milestone = events[0]
    assert milestone["payload"]["milestone"] == "first_met"
    assert milestone["payload"]["stage"] == "acquaintance"
    assert milestone["causation_id"] == dm["event_id"]

    changed = next(e for e in events if e["type"] == "relationship.state.changed")
    dims = changed["payload"]["dimensions"]
    assert dims["trust"] > 0 and dims["intimacy"] > 0  # 지지 DM은 신뢰·친밀을 만든다
    assert changed["payload"]["salience"] > 0

    # Redis 엣지 상태가 이벤트와 일치하는 방향으로 남았다
    state = await RelationshipAdapter(redis).load(WORLD, "a_aria_kim", PLAYER)
    assert state is not None and state.stage == "acquaintance"
    assert state.dimensions["trust"] > 0


async def test_first_met_fires_once(conn, redis, nc, ai_service):  # noqa: F811
    mailbox = Mailbox(redis)
    phases = make_phases(nc, redis, ai_service, mailbox)

    first = player_envelope("player.dm.sent", {"text": "안녕하세요"})
    await mailbox.push(WORLD, "a_aria_kim", first)
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)
    second = player_envelope("player.dm.sent", {"text": "또 왔어요"})
    await mailbox.push(WORLD, "a_aria_kim", second)
    await run_tick(conn, phases, CLOCK, WORLD, tick=1, head=head)

    events = [s.envelope for s in await read_stream(conn, WORLD, "relationship", EDGE)]
    milestones = [e for e in events if e["type"] == "relationship.milestone.reached"]
    assert len(milestones) == 1  # 처음은 한 번뿐이다
