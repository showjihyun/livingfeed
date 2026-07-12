"""reflection 검증 — 상태 패턴 → 신념, 갱신 규율 (ADR-008).

순수 규칙 + (PG+Redis+NATS 게이트) tick 통합.
"""

from datetime import UTC, datetime

from lf_actor.client import AiRuntimeClient
from lf_actor.emotion import EmotionAdapter
from lf_actor.mailbox import Mailbox
from lf_actor.memory import WorkingMemory
from lf_actor.persona import load_persona
from lf_actor.phases import ActorPhases
from lf_actor.reflection import RESTATE_DELTA, Belief, BeliefLedger, derive_beliefs
from lf_actor.relationship import RelationshipAdapter
from lf_emotion import EmotionInstance, EmotionState
from lf_eventstore import read_stream
from lf_relationship import RelationshipState
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick

from .conftest import PERSONAS_DIR
from .test_mailbox import player_envelope
from .test_phases import ai_service, nc  # noqa: F401 — 픽스처 재사용

WORLD = "w_test"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))


def edge(trust=0.0, intimacy=0.0, resentment=0.0) -> RelationshipState:
    state = RelationshipState()
    dims = dict(state.dimensions)
    dims.update({"trust": trust, "intimacy": intimacy, "resentment": resentment})
    return RelationshipState(dimensions=dims, stage="acquaintance", salience=0.2)


def test_supporter_belief_from_warm_edge():
    beliefs = derive_beliefs(
        EmotionState(), {"p_observer_0417": edge(trust=0.3, intimacy=0.2)},
        name_map={},
    )
    [supporter] = [b for b in beliefs if b.kind == "supporter"]
    assert supporter.about_id == "p_observer_0417"
    assert "힘이 되는 사람" in supporter.statement
    assert supporter.confidence == round(0.5 * 0.3 + 0.5 * 0.2, 4)


def test_threat_belief_survives_trust_recovery():
    # 원한은 신뢰와 독립 축이다 (ADR-016) — 신뢰가 높아도 threat 신념이 선다
    beliefs = derive_beliefs(
        EmotionState(), {"a_editor_choi": edge(trust=0.4, intimacy=0.3, resentment=0.5)},
    )
    kinds = {b.kind for b in beliefs}
    assert "supporter" in kinds and "threat" in kinds  # 애증 — 서사 재료


def test_felt_support_traces_source_event():
    emotion = EmotionState(
        emotions=(
            EmotionInstance(
                type="gratitude", intensity=0.83,
                target_id="p_observer_0417",
                source_event="01JZK7Q3W0000000000000000G",
            ),
        )
    )
    [belief] = [b for b in derive_beliefs(emotion, {}) if b.kind == "felt_support"]
    assert belief.source_event_ids == ["01JZK7Q3W0000000000000000G"]  # 감사 추적 (규칙 4)


def test_neutral_states_form_no_beliefs():
    assert derive_beliefs(EmotionState(), {"a_x": edge(trust=0.1, intimacy=0.05)}) == []


async def test_ledger_restates_only_on_meaningful_change(redis):
    ledger = BeliefLedger(redis)
    belief = Belief("문장", "supporter", 0.5, "p_x", [])
    assert await ledger.changed(WORLD, "a_aria_kim", belief)  # 처음은 발행
    await ledger.record(WORLD, "a_aria_kim", belief)

    barely = Belief("문장", "supporter", 0.5 + RESTATE_DELTA / 2, "p_x", [])
    assert not await ledger.changed(WORLD, "a_aria_kim", barely)  # 미세 변화는 침묵

    shifted = Belief("문장", "supporter", 0.5 + RESTATE_DELTA, "p_x", [])
    assert await ledger.changed(WORLD, "a_aria_kim", shifted)  # 의미 있는 변화만 재발행


async def test_reflection_forms_belief_through_ticks(conn, redis, nc, ai_service):  # noqa: F811
    """DM 여러 통 → 관계가 데워지고 → reflection tick에 신념이 이벤트로 선다."""
    mailbox = Mailbox(redis)
    phases = ActorPhases(
        [load_persona(PERSONAS_DIR / "aria-kim.yaml")],
        ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis),
        mailbox=mailbox,
        emotion=EmotionAdapter(redis),
        relationship=RelationshipAdapter(redis),
        belief_ledger=BeliefLedger(redis),
        reflection_interval=4,  # 테스트: 4 tick마다 곱씹는다
    )
    head = 0
    for tick in range(1, 5):
        await mailbox.push(
            WORLD, "a_aria_kim",
            player_envelope("player.dm.sent", {"text": f"오늘도 응원해요 ({tick})"}),
        )
        head = await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=head)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", "a_aria_kim")]
    beliefs = [e for e in events if e["type"] == "actor.belief.formed"]
    assert beliefs, "reflection tick(4)에서 신념이 형성됐어야 한다"
    kinds = {b["payload"]["kind"] for b in beliefs}
    assert kinds & {"supporter", "felt_support"}
    for belief in beliefs:
        assert belief["payload"]["about_id"] == "p_observer_0417"

    # Working Memory에 곱씹음이 남아 다음 결정의 컨텍스트가 된다
    recent = await WorkingMemory(redis).recent(WORLD, "a_aria_kim")
    assert any("곱씹은 생각" in m for m in recent)
