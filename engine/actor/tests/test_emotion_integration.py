"""감정 경로 통합 — 개입 → appraise → actor.emotion.shifted 적재 (ADR-015).

PG+Redis(+NATS) 필요 (없으면 skip — conftest 참고).
"""

from datetime import UTC, datetime

from lf_actor.client import AiRuntimeClient
from lf_actor.emotion import EmotionAdapter
from lf_actor.mailbox import Mailbox
from lf_actor.memory import WorkingMemory
from lf_actor.persona import load_persona
from lf_actor.phases import ActorPhases
from lf_eventstore import read_stream
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick

from .conftest import PERSONAS_DIR
from .test_mailbox import player_envelope
from .test_phases import ai_service, nc  # noqa: F401 — 픽스처 재사용

WORLD = "w_test"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))


def make_phases(nc, redis, env: str, mailbox: Mailbox) -> ActorPhases:  # noqa: F811
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    return ActorPhases(
        [aria],
        ai=AiRuntimeClient(nc, env, timeout_s=5),
        memory=WorkingMemory(redis),
        mailbox=mailbox,
        emotion=EmotionAdapter(redis),
    )


async def test_dm_shifts_emotion_and_appends_event(conn, redis, nc, ai_service):  # noqa: F811
    mailbox = Mailbox(redis)
    dm = player_envelope("player.dm.sent", {"text": "기사 응원해요. 진실은 힘이 세요."})
    await mailbox.push(WORLD, "a_aria_kim", dm)

    phases = make_phases(nc, redis, ai_service, mailbox)
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", "a_aria_kim")]
    types = [e["type"] for e in events]
    # 감정 변화(원인 상태)가 응답·행동보다 앞서 기록된다
    assert types == ["actor.emotion.shifted", "actor.message.sent", "actor.action.performed"]

    shift = events[0]
    assert shift["causation_id"] == dm["event_id"]
    assert shift["payload"]["mood"]["pleasure"] > 0  # 지지 DM은 기분을 끌어올린다
    [gratitude] = [e for e in shift["payload"]["emotions"] if e["type"] == "gratitude"]
    assert gratitude["target_id"] == "p_observer_0417"  # '누구 때문에'가 남는다

    # 감정 상태가 Redis에 남아 다음 tick의 컨텍스트가 된다
    state = await EmotionAdapter(redis).load(WORLD, "a_aria_kim")
    assert state.emotions and state.emotions[0].type == "gratitude"

    # Working Memory에 기분 요약이 주입됐다 (ADR-015 §행동 연결)
    recent = await WorkingMemory(redis).recent(WORLD, "a_aria_kim")
    assert any("지금 기분" in m for m in recent)


async def test_emotion_decays_across_ticks(conn, redis, nc, ai_service):  # noqa: F811
    mailbox = Mailbox(redis)
    await mailbox.push(
        WORLD, "a_aria_kim",
        player_envelope("player.reaction.added",
                        {"post_id": "01JZK7Q3W0000000000000000F", "kind": "like"}),
    )
    phases = make_phases(nc, redis, ai_service, mailbox)
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    adapter = EmotionAdapter(redis)
    after_tick0 = await adapter.load(WORLD, "a_aria_kim")
    assert after_tick0.emotions  # 좋아요 → joy 인스턴스

    # 개입 없는 tick들이 흐르면 감정이 감쇠한다 (CONSOLIDATE)
    for tick in range(1, 4):
        head = await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=head)
    later = await adapter.load(WORLD, "a_aria_kim")
    assert later.emotions[0].intensity < after_tick0.emotions[0].intensity
