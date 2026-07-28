"""L2 규칙 재실행 — 세계에서 LLM을 뺀 부분은 비트 단위로 결정적이다 (ADR-021 §4).

L3가 못 주는 것을 이 등급만 준다: 다시 돌려서 같은 답이 나오는지 **실제로**
확인할 수 있다. 그래서 여기서 검증하는 것은 두 가지다 — 같은 입력이면 통과하고,
규칙이 조용히 바뀌면 잡힌다.
"""

from datetime import UTC, datetime

import pytest
from lf_actor.arc import Arc
from lf_actor.persona import load_persona
from lf_actor.replay_rules import (
    UnsupportedRule,
    rule_id_of,
    verify_rule_event,
)
from lf_actor.rules import fallback_action, fallback_follow_up, routine_action
from lf_eventstore import ReplayTier, UnverifiableTier, assert_verifiable

from .conftest import PERSONAS_DIR
from .test_phases import ai_service, nc  # noqa: F401 — 픽스처 재사용

CLOCK_GENESIS = datetime(2026, 3, 1, tzinfo=UTC)
TICK = 42
TRACE = "t-l2"


def persona():
    return load_persona(PERSONAS_DIR / "aria-kim.yaml")


def envelope(payload: dict, rule_id: str, tick: int = TICK) -> dict:
    """적재된 봉투 모양 — 검증기가 실제로 받는 형태."""
    return {
        "type": "actor.action.performed",
        "tick": tick,
        "provenance": {"kind": "derived", "rule_id": rule_id},
        "payload": payload,
    }


# --- 같은 입력이면 통과한다 -----------------------------------------------------


def test_fallback_action_reexecutes_bit_identical():
    p = persona()
    recorded = fallback_action(p, TICK, TRACE)
    verdict = verify_rule_event(envelope(recorded, "actor.rules:fallback_action"), p)
    assert verdict
    assert verdict.differing_keys == ()


def test_routine_action_reexecutes_with_its_arc():
    """아크가 일과의 마지막 결을 준다 — 입력이 하나 더 있을 뿐 결정성은 같다."""
    p = persona()
    arc = Arc(stage="prime", intention="쓰던 글을 끝까지 밀어붙인다")
    recorded = routine_action(p, TICK, TRACE, arc=arc)
    assert verify_rule_event(
        envelope(recorded, "actor.rules:routine_action"), p, arc=arc
    )


def test_follow_up_reexecutes_with_its_fragment():
    p = persona()
    recorded = fallback_follow_up(p, TICK, TRACE, fragment="그 말이 오래 남았어요")
    assert verify_rule_event(
        envelope(recorded, "actor.rules:fallback_follow_up"),
        p,
        fragment="그 말이 오래 남았어요",
    )


# --- 조용한 변화가 잡힌다 -------------------------------------------------------


def test_changed_rule_output_is_caught():
    """규칙이 바뀌면(또는 이벤트가 손상되면) 어긋난 키가 그대로 드러난다."""
    p = persona()
    recorded = {**fallback_action(p, TICK, TRACE), "intent": "누군가 손댄 문장"}
    verdict = verify_rule_event(envelope(recorded, "actor.rules:fallback_action"), p)
    assert not verdict
    assert verdict.differing_keys == ("intent",)


def test_wrong_arc_is_caught():
    """상태 입력이 달라도 잡힌다 — '그때 그 아크'가 아니면 같은 일과가 아니다."""
    p = persona()
    recorded = routine_action(
        p, TICK, TRACE, arc=Arc(stage="prime", intention="A")
    )
    verdict = verify_rule_event(
        envelope(recorded, "actor.rules:routine_action"),
        p,
        arc=Arc(stage="prime", intention="B"),
    )
    assert not verdict


def test_different_tick_is_caught():
    """tick이 표현을 고른다 — 같은 인물이라도 다른 tick이면 다른 행동이다."""
    p = persona()
    recorded = fallback_action(p, TICK, TRACE)
    verdict = verify_rule_event(envelope(recorded, "actor.rules:fallback_action", tick=TICK + 1), p)
    assert not verdict


# --- 검증하지 못하는 것은 통과로 세지 않는다 --------------------------------------


def test_generated_events_are_refused():
    """LLM 생성물은 L2의 대상이 아니다 — 재실행해서 같은 답이 나올 리 없다."""
    event = {
        "type": "actor.action.performed",
        "tick": TICK,
        "provenance": {"kind": "generated", "trace_id": TRACE},
        "payload": fallback_action(persona(), TICK, TRACE),
    }
    with pytest.raises(UnsupportedRule, match="derived"):
        verify_rule_event(event, persona())


def test_unregistered_rule_is_refused_not_passed():
    """상태에 의존하는 규칙(derive_beliefs 등)은 아직 닫히지 않는다 — 거절이 정답이다."""
    with pytest.raises(UnsupportedRule):
        verify_rule_event(
            envelope(fallback_action(persona(), TICK, TRACE), "reflection:supporter"),
            persona(),
        )


def test_follow_up_without_its_fragment_is_refused():
    """여운 조각은 Redis 상태다 — 없으면 '재구성'하지 말고 거절한다."""
    p = persona()
    recorded = fallback_follow_up(p, TICK, TRACE, fragment="원문")
    with pytest.raises(UnsupportedRule, match="fragment"):
        verify_rule_event(envelope(recorded, "actor.rules:fallback_follow_up"), p)


def test_l2_is_a_verifiable_tier_but_l3_is_not():
    """검증기의 첫 줄이 등급 계약을 확인한다 (ADR-021 §4)."""
    assert assert_verifiable(ReplayTier.RULE_REEXECUTION).entry_point
    with pytest.raises(UnverifiableTier):
        assert_verifiable(ReplayTier.LLM_REEXECUTION)


def test_rule_id_of_reads_the_provenance_contract():
    assert rule_id_of(envelope({}, "actor.rules:fallback_action")) == "actor.rules:fallback_action"
    assert rule_id_of({"provenance": {"kind": "generated", "trace_id": "t"}}) is None
    assert rule_id_of({}) is None


# --- 실제 tick이 남긴 규칙 이벤트가 그대로 재실행된다 ------------------------------


async def test_recorded_fallback_action_replays_from_the_real_tick(
    conn, redis, nc, ai_service,  # noqa: F811
):
    """세계가 실제로 남긴 봉투를 L2로 되돌린다 — 라벨과 규칙이 짝이 맞아야 성립한다.

    rule_id를 tier로만 뭉뚱그리면(셋 다 cold_rule) 검증기가 다른 규칙을 돌려 놓고
    '어긋났다'고 보고한다 — 없는 회귀를 만드는 셈이다 (_rule_name의 존재 이유).
    """
    from lf_actor.client import AiRuntimeClient
    from lf_actor.memory import WorkingMemory
    from lf_actor.phases import ActorPhases
    from lf_eventstore import read_stream
    from lf_tick.clock import TickClock
    from lf_tick.engine import run_tick

    world = "w_l2"
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    # env를 비워 AI Runtime 무응답 → 규칙 폴백 경로가 확정적으로 탄다
    phases = ActorPhases(
        [aria], ai=AiRuntimeClient(nc, "nosuchenv", timeout_s=2), memory=WorkingMemory(redis)
    )
    await run_tick(conn, phases, TickClock(genesis=CLOCK_GENESIS), world, tick=0, head=0)

    stored = await read_stream(conn, world, "actor", aria.id)
    [action] = [s.envelope for s in stored if s.envelope["type"] == "actor.action.performed"]
    assert action["provenance"]["rule_id"] == "actor.rules:fallback_action"

    verdict = verify_rule_event(action, aria)
    assert verdict, verdict.differing_keys
