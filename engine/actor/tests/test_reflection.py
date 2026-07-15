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
from lf_actor.reflection import (
    RESTATE_DELTA,
    Belief,
    BeliefLedger,
    derive_beliefs,
    insight_schema,
    insight_to_belief,
)
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


def test_insight_schema_bounds_kind_and_about():
    schema = insight_schema(["a_junho_park", "p_observer_0417"])
    assert schema["properties"]["kind"]["enum"] == ["self_image", "world_view", "person_insight"]
    assert schema["properties"]["about_actor_id"]["enum"] == [
        "a_junho_park", "p_observer_0417", None,
    ]
    assert schema["additionalProperties"] is False


def test_insight_to_belief_enforces_hard_rules():
    known = {"a_junho_park"}
    valid = {"statement": "나는 마감 앞에서 무너지지 않는다", "kind": "self_image",
             "confidence": 0.72, "about_actor_id": None}
    belief = insight_to_belief(valid, known)
    assert belief is not None and belief.kind == "self_image" and belief.about_id is None

    # 환각 대상은 끊는다 — 아는 사람 밖 about은 null로 (자기/세계 통찰은 유지)
    ghost = insight_to_belief({**valid, "about_actor_id": "a_ghost"}, known)
    assert ghost is not None and ghost.about_id is None
    # 대상 없는 인물 통찰은 통찰이 아니다
    assert insight_to_belief(
        {**valid, "kind": "person_insight", "about_actor_id": "a_ghost"}, known
    ) is None
    # 유효한 인물 통찰은 대상이 남는다
    person = insight_to_belief(
        {**valid, "kind": "person_insight", "about_actor_id": "a_junho_park"}, known
    )
    assert person is not None and person.about_id == "a_junho_park"
    # 확신 없는 신념·미지 분류·빈 명제는 신념이 아니다
    assert insight_to_belief({**valid, "confidence": 0.0}, known) is None
    assert insight_to_belief({**valid, "kind": "prophecy"}, known) is None
    assert insight_to_belief({**valid, "statement": "  "}, known) is None
    assert insight_to_belief({**valid, "confidence": 7}, known).confidence == 1.0  # 클램프


class _StubReflectAi:
    """reflect만 통찰을 내는 스텁 — decide/converse는 실패해 규칙 폴백을 탄다."""

    def __init__(self, insight: dict) -> None:
        self._insight = insight
        self.last_user: str | None = None

    async def decide_action(self, *args, **kwargs):
        return None

    async def converse(self, *args, **kwargs):
        return None

    async def reflect(self, bundle, schema, *, actor_id, tick):
        self.last_user = bundle.user
        return self._insight


async def test_llm_insight_joins_rule_beliefs(conn, redis):
    """LLM reflection — 작업 기억을 곱씹은 통찰이 규칙 신념과 같은 계약으로 선다
    (ADR-008). 이벤트·대장·작업기억 주입이 규칙 경로와 동일하다."""
    insight = {
        "statement": "나는 마감 앞에서 무너지지 않는 사람이다",
        "kind": "self_image", "confidence": 0.72, "about_actor_id": None,
    }
    ai = _StubReflectAi(insight)
    phases = ActorPhases(
        [load_persona(PERSONAS_DIR / "aria-kim.yaml")],
        ai=ai,
        memory=WorkingMemory(redis),
        emotion=EmotionAdapter(redis),
        relationship=RelationshipAdapter(redis),
        belief_ledger=BeliefLedger(redis),
        reflection_interval=2,
    )
    await WorkingMemory(redis).add(WORLD, "a_aria_kim", "tick 1: 밤을 새워 마감을 지켰다")
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=1, head=0)
    await run_tick(conn, phases, CLOCK, WORLD, tick=2, head=head)  # reflection tick

    assert ai.last_user is not None and "아는 사람들" in ai.last_user  # 로스터 그라운딩
    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", "a_aria_kim")]
    [belief] = [e for e in events if e["type"] == "actor.belief.formed"]
    assert belief["payload"]["kind"] == "self_image"
    assert belief["payload"]["statement"] == insight["statement"]
    assert belief["payload"]["confidence"] == 0.72
    assert belief["payload"]["about_id"] is None
    # 곱씹음이 작업 기억으로 — 다음 결정의 컨텍스트 (규칙 경로와 같은 계약)
    recent = await WorkingMemory(redis).recent(WORLD, "a_aria_kim")
    assert any("곱씹은 생각" in m for m in recent)


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
