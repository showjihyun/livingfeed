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
from lf_eventstore import new_ulid, read_stream
from lf_relationship import RelationshipState
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


def inject_stage_action(phases: ActorPhases, action_kind: str, target_id: str) -> None:
    """decide 직후에 상위 전이 행동 의도를 심는다 — confess/sever는 LLM decide만
    고르는 행동이라(규칙 폴백은 절대 안 고른다) rule 프로바이더 테스트에선 'LLM이
    이 행동을 골랐다'를 주입으로 재현한다. resolve→CONSOLIDATE는 실제 경로다."""
    original = phases.decide

    async def decide_then_inject(ctx):
        decided = await original(ctx)
        phases._intents.append((
            "a_aria_kim", "hot",
            {
                "action_kind": action_kind,
                "intent": "오래 품은 마음을 매듭짓는다",
                "target_actor_id": target_id,
                "location_id": None,
                "params": {},
                "decision_trace": {"trace_id": new_ulid(), "tier": "hot"},
            },
        ))
        return decided

    phases.decide = decide_then_inject


def seeded_edge(**kwargs) -> RelationshipState:
    base = RelationshipState()
    dims = {k: kwargs.pop(k) for k in list(kwargs) if k in base.dimensions}
    return RelationshipState(dimensions={**base.dimensions, **dims}, pending=base.pending, **kwargs)


async def test_confess_transitions_both_edges_to_romantic(conn, redis, nc, ai_service):  # noqa: F811
    """confess 응고 — 수락되면 양쪽 stage가 romantic이 되고 마일스톤이 남는다 (ADR-016).

    수락 조건은 대상→행위자 엣지의 마음: 민지→아리아에 신뢰가 닿아 있어야 한다.
    """
    rel = RelationshipAdapter(redis)
    await rel.save(
        WORLD, "a_minji_kim", "a_aria_kim",
        seeded_edge(trust=0.5, intimacy=0.3, stage="friend", salience=0.5),
    )
    await rel._register_edge(WORLD, "a_minji_kim", "a_aria_kim")

    phases = make_phases(nc, redis, ai_service, Mailbox(redis))
    inject_stage_action(phases, "confess", "a_minji_kim")
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    events = [
        s.envelope
        for s in await read_stream(conn, WORLD, "relationship", "a_aria_kim|a_minji_kim")
    ]
    milestones = [e for e in events if e["type"] == "relationship.milestone.reached"]
    # 아리아→민지 엣지는 이번에 처음 생겼다(first_met) — 그 위에 고백 수락이 쌓인다
    assert [m["payload"]["milestone"] for m in milestones] == ["first_met", "confession_accepted"]
    accepted = milestones[-1]
    assert accepted["payload"]["stage"] == "romantic"
    assert accepted["payload"]["note"]  # "어쩌다 이렇게 됐는지"의 한 줄

    # 마일스톤의 원인은 확정된 confess 행동 이벤트다 (감사 체인)
    actions = [s.envelope for s in await read_stream(conn, WORLD, "actor", "a_aria_kim")]
    confess = next(
        e for e in actions
        if e["type"] == "actor.action.performed" and e["payload"]["action_kind"] == "confess"
    )
    assert accepted["causation_id"] == confess["event_id"]

    # 양쪽 엣지 모두 연인이 됐다 — 저장 상태가 이벤트와 일치한다
    mine = await rel.load(WORLD, "a_aria_kim", "a_minji_kim")
    theirs = await rel.load(WORLD, "a_minji_kim", "a_aria_kim")
    assert mine is not None and mine.stage == "romantic"
    assert theirs is not None and theirs.stage == "romantic"


async def test_sever_returns_both_edges_to_stranger(conn, redis, nc, ai_service):  # noqa: F811
    """sever 응고 — 양쪽 stage가 stranger로 회귀하되 salience는 남는다 (마음의 흔적)."""
    rel = RelationshipAdapter(redis)
    for a, b in (("a_aria_kim", "a_minji_kim"), ("a_minji_kim", "a_aria_kim")):
        await rel.save(
            WORLD, a, b, seeded_edge(trust=0.5, intimacy=0.6, stage="close_friend", salience=0.7)
        )
        await rel._register_edge(WORLD, a, b)

    phases = make_phases(nc, redis, ai_service, Mailbox(redis))
    inject_stage_action(phases, "sever", "a_minji_kim")
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    events = [
        s.envelope
        for s in await read_stream(conn, WORLD, "relationship", "a_aria_kim|a_minji_kim")
    ]
    milestones = [e for e in events if e["type"] == "relationship.milestone.reached"]
    assert [m["payload"]["milestone"] for m in milestones] == ["severed"]
    assert milestones[0]["payload"]["stage"] == "stranger"

    mine = await rel.load(WORLD, "a_aria_kim", "a_minji_kim")
    theirs = await rel.load(WORLD, "a_minji_kim", "a_aria_kim")
    assert mine is not None and mine.stage == "stranger"
    assert theirs is not None and theirs.stage == "stranger"
    assert mine.salience > 0.6  # 끊었지만 비중은 남는다 — 흔적


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
