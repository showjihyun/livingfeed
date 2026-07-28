"""인지 예산 — 인물별로 다른 인지 자원이 실험 변수가 된다 (ADR-021 §3).

두 가지를 함께 지킨다:
  (1) 오버라이드가 없으면 세계는 지금과 똑같이 돈다 — 관측 장치가 시뮬레이션을
      바꾸면 그 세계는 대조군이 아니다.
  (2) 오버라이드를 걸면 실제로 회상과 기억이 줄어든다 — 손잡이가 돌아가지
      않으면 실험 변수가 아니다.
"""

from datetime import UTC, datetime

import pytest
from lf_actor.cognition import (
    DEFAULT_MEMORY_TOKENS,
    DEFAULT_RECALL_SLOTS,
    CognitiveBudget,
    CognitiveBudgets,
)
from lf_actor.context import BUDGET_EPISODES, BUDGET_WORKING, WorldContext, build
from lf_actor.persona import load_persona
from lf_actor.rhythm import default_params
from lf_actor.semantic import Recollection

from .conftest import PERSONAS_DIR

WORLD = WorldContext(world_id="w_test", tick=7, world_time=datetime(2026, 3, 1, tzinfo=UTC))


def persona():
    return load_persona(PERSONAS_DIR / "aria-kim.yaml")


# --- 기본값은 현행 동작이다 -----------------------------------------------------


def test_shipped_defaults_match_the_current_world():
    """params.yaml의 티어 기본이 지금까지 쓰던 상수와 같아야 한다."""
    budgets = CognitiveBudgets.from_params(default_params())
    hot = budgets.for_actor("a_anyone", "hot")
    assert hot.recall_slots == DEFAULT_RECALL_SLOTS
    assert hot.memory_tokens == DEFAULT_MEMORY_TOKENS == BUDGET_EPISODES + BUDGET_WORKING
    assert not budgets.has_overrides  # 저장소 기본은 대조군이다


def test_unknown_tier_falls_back_to_base():
    """티어가 표에 없어도 인물이 멎지 않는다 — 전방 호환 (lifestyle 선례)."""
    budgets = CognitiveBudgets.from_params(default_params())
    assert budgets.for_actor("a_anyone", "새로운티어") == CognitiveBudget()


def test_build_without_budget_is_unchanged():
    """memory_tokens를 주지 않은 조립은 예산 도입 전과 같은 결과여야 한다."""
    plain = build(persona(), ["tick 6: 나는 work"], WORLD, trace_id="t")
    explicit = build(
        persona(), ["tick 6: 나는 work"], WORLD, trace_id="t",
        memory_tokens=BUDGET_EPISODES + BUDGET_WORKING,
    )
    assert plain.digest == explicit.digest


# --- 손잡이가 실제로 돈다 -------------------------------------------------------


def test_smaller_memory_budget_actually_shrinks_the_context():
    """기억 예산을 줄이면 프롬프트의 기억이 줄어든다."""
    working = [f"tick {i}: 긴 기억 " + "가" * 200 for i in range(40, 0, -1)]
    roomy = build(persona(), working, WORLD, trace_id="t", memory_tokens=1800)
    tight = build(persona(), working, WORLD, trace_id="t", memory_tokens=200)

    def working_tokens(bundle):
        [section] = [s for s in bundle.sections if s.kind == "working"]
        return section.token_count

    assert working_tokens(tight) < working_tokens(roomy)
    assert roomy.digest != tight.digest  # 다른 예산은 다른 컨텍스트다


def test_memory_split_keeps_both_memories_alive():
    """예산이 아무리 작아도 한쪽 기억만 남기지 않는다 — 그러면 다른 실험이 된다."""
    episodes = [Recollection(f"01JZK7Q3W000000000000000{i:02X}", "기억 " * 50) for i in range(5)]
    bundle = build(
        persona(), ["tick 1: 무언가 있었다"], WORLD, trace_id="t",
        episodes=episodes, memory_tokens=2,  # 하한
    )
    kinds = {s.kind for s in bundle.sections}
    assert {"episodes", "working"} <= kinds
    for kind in ("episodes", "working"):
        [section] = [s for s in bundle.sections if s.kind == kind]
        assert section.text  # 비어 있지 않다


# --- 오버라이드는 데이터다 ------------------------------------------------------


def test_override_applies_to_only_the_named_actor():
    """실험 대상만 달라진다 — 나머지는 대조군으로 남는다."""
    budgets = CognitiveBudgets(
        tiers={"hot": {"recall_slots": 3, "memory_tokens": 1800, "calls_per_tick": 4}},
        overrides={"a_shallow": {"recall_slots": 1}},
    )
    assert budgets.for_actor("a_shallow", "hot").recall_slots == 1
    assert budgets.for_actor("a_normal", "hot").recall_slots == 3
    # 준 항목만 덮는다 — 나머지는 티어 기본이 남는다
    assert budgets.for_actor("a_shallow", "hot").memory_tokens == 1800
    assert budgets.has_overrides


@pytest.mark.parametrize("field", ["recall_slots", "memory_tokens", "calls_per_tick"])
def test_zero_budget_is_refused(field: str):
    """0은 '자원이 적은 인물'이 아니라 '기억이 없는·존재하지 않는 인물'이다."""
    with pytest.raises(ValueError, match=field):
        CognitiveBudget(**{field: 0})


def test_budget_travels_into_the_decision_record():
    """어떤 예산으로 내린 결정인지가 남아야 실험 결과를 예산과 이어 읽는다."""
    assert CognitiveBudget(recall_slots=1, memory_tokens=200, calls_per_tick=2).to_json() == {
        "recall_slots": 1,
        "memory_tokens": 200,
        "calls_per_tick": 2,
    }
