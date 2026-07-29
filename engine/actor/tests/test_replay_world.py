"""세계를 다시 돌린다 — L0 재생 러너 (ADR-021 §4).

관계 값처럼 이벤트 없이 흔들리는 상태는 복원 대상이 아니라 **재생의 부산물**이
된다. 대조는 훅 없이 이뤄진다: 재생 세계도 자기 actor.decision.made를 내므로,
두 세계의 결정을 짝지어 digest를 맞추면 된다.
"""

from datetime import UTC, datetime

import pytest
from lf_actor.client import AiRuntimeClient
from lf_actor.emotion import EmotionAdapter
from lf_actor.mailbox import Mailbox
from lf_actor.memory import WorkingMemory
from lf_actor.persona import load_persona
from lf_actor.phases import ActorPhases
from lf_actor.relationship import RelationshipAdapter
from lf_actor.replay_world import (
    ReplayRefused,
    compare_decisions,
    external_schedule,
    replay_world,
)
from lf_eventstore import NewEvent, Provenance, TracePolicy, append, new_ulid
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick

from .conftest import PERSONAS_DIR
from .test_phases import ai_service, nc  # noqa: F401 — 픽스처 재사용

SOURCE = "w_src_replay"
TARGET = "w_replayed"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))


def aria():
    return load_persona(PERSONAS_DIR / "aria-kim.yaml")


def wiring(ai, world_id: str, redis, mailbox=None) -> ActorPhases:
    """원본과 재생이 **같은 배선**이어야 같은 세계가 된다 (로그에 없는 것)."""
    return ActorPhases(
        [aria()], ai=ai, memory=WorkingMemory(redis), mailbox=mailbox,
        emotion=EmotionAdapter(redis), relationship=RelationshipAdapter(redis),
        trace_policy=TracePolicy.research(),
    )


async def test_quiet_world_replays_to_the_same_decisions(conn, redis, nc, ai_service):  # noqa: F811
    """조용한 세계는 그대로 재생된다 — 같은 결정, 같은 컨텍스트."""
    phases = wiring(AiRuntimeClient(nc, ai_service, timeout_s=5), SOURCE, redis)
    head = 0
    for tick in range(3):
        head = await run_tick(conn, phases, CLOCK, SOURCE, tick=tick, head=head)

    result = await replay_world(
        conn, source_world_id=SOURCE, target_world_id=TARGET,
        make_phases=lambda ai, world: wiring(ai, world, redis),
        clock=CLOCK, redis=redis, through_tick=2,
    )
    assert result.unused_recordings == 0  # 원본과 같은 횟수로 불렀다
    assert result.schedule_exact

    report = await compare_decisions(
        conn, SOURCE, TARGET,
        schedule_exact=result.schedule_exact,
        unused_recordings=result.unused_recordings,
    )
    assert report.ok, report.summary()
    assert report.summary()["verified"] == report.summary()["decisions"] > 0


async def test_replay_refuses_to_overwrite_a_world(conn, redis):
    with pytest.raises(ReplayRefused):
        await replay_world(
            conn, source_world_id=SOURCE, target_world_id=SOURCE,
            make_phases=lambda ai, world: None, clock=CLOCK, redis=redis, through_tick=0,
        )


async def test_unknown_perception_time_makes_the_replay_approximate(conn, redis):
    """마음을 흔들지 않은 개입은 언제 닿았는지 모른다 — 숨기지 않고 따로 센다."""
    event_id = new_ulid()
    await append(
        conn, "services.gateway",
        [NewEvent(
            world_id=SOURCE, stream="player", stream_key="p_quiet",
            type="player.reaction.added", tick=0, event_id=event_id,
            provenance=Provenance.authored("p_quiet"),
            payload={"player_id": "p_quiet", "target_actor_id": aria().id,
                     "post_id": new_ulid(), "kind": "like"},
        )],
        expected_head=0,
    )
    schedule = await external_schedule(conn, SOURCE)
    assert not schedule.exact
    assert event_id in schedule.unscheduled


async def test_approximate_replay_never_reports_drift(conn, redis, nc, ai_service):  # noqa: F811
    """근사 재생의 발산은 '검증 실패'가 아니다 — 없는 사고를 만들지 않는다."""
    phases = wiring(AiRuntimeClient(nc, ai_service, timeout_s=5), SOURCE, redis)
    await run_tick(conn, phases, CLOCK, SOURCE, tick=0, head=0)
    await replay_world(
        conn, source_world_id=SOURCE, target_world_id=TARGET,
        make_phases=lambda ai, world: wiring(ai, world, redis),
        clock=CLOCK, redis=redis, through_tick=0,
    )

    report = await compare_decisions(
        conn, SOURCE, TARGET, schedule_exact=False, unused_recordings=0
    )
    assert report.mismatched == []      # 어긋남으로 세지 않는다
    assert not report.ok                # 그러나 통과도 아니다
    assert "external_schedule" in report.unverifiable


async def test_player_intervention_is_reinjected_at_its_perception_tick(
    conn, redis, nc, ai_service,  # noqa: F811
):
    """개입은 원본이 지각한 tick에 다시 들어간다 — 그 시점의 원천은 감정 변화다."""
    event_id = new_ulid()
    dm_payload = {"player_id": "p_watcher", "target_actor_id": aria().id,
                  "text": "기획안 얘기 봤어요. 응원해요."}
    await append(
        conn, "services.gateway",
        [NewEvent(
            world_id=SOURCE, stream="player", stream_key="p_watcher",
            type="player.dm.sent", tick=0, event_id=event_id,
            provenance=Provenance.authored("p_watcher"), payload=dm_payload,
        )],
        expected_head=0,
    )
    mailbox = Mailbox(redis)
    await mailbox.push(SOURCE, aria().id, {
        "event_id": event_id, "stream": "player", "type": "player.dm.sent",
        "schema_version": 1, "world_id": SOURCE, "actor_id": None, "tick": 0,
        "occurred_at": "2026-03-01T00:00:00Z", "causation_id": None,
        "correlation_id": event_id,
        "provenance": {"kind": "authored", "author_id": "p_watcher"},
        "payload": dm_payload,
    })
    phases = wiring(AiRuntimeClient(nc, ai_service, timeout_s=5), SOURCE, redis, mailbox)
    head = 0
    for tick in range(2):
        head = await run_tick(conn, phases, CLOCK, SOURCE, tick=tick, head=head)

    # 그 DM이 tick 0에 지각됐음이 감정 변화로 남았다
    schedule = await external_schedule(conn, SOURCE)
    assert schedule.exact, schedule.unscheduled
    assert [e["event_id"] for e in schedule.by_tick[0]] == [event_id]
