"""페르소나·Context Fabric·규칙 폴백의 순수 로직 검증 (ADR-009/012)."""

from datetime import UTC, datetime

from jsonschema import Draft202012Validator
from lf_actor.context import WorldContext, build
from lf_actor.persona import load_persona, load_personas
from lf_actor.rules import fallback_action
from lf_schemas import registry

from .conftest import PERSONAS_DIR

ACTION_SCHEMA = registry.payload_schema("actor.action.performed")
WORLD = WorldContext(world_id="w_test", tick=7, world_time=datetime(2026, 3, 1, 12, tzinfo=UTC))


def test_load_aria_persona():
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    assert aria.id == "a_aria_kim"
    assert aria.name == "김아리"
    assert "탐사보도" in aria.identity_core
    assert aria.needs_bias["achievement"] == 0.90
    assert aria.goals[0]["id"] == "g_expose_corruption"


def test_load_personas_directory():
    personas = load_personas(PERSONAS_DIR)
    assert any(p.id == "a_aria_kim" for p in personas)


def test_context_bundle_is_deterministic_and_ordered():
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    working = ["tick 6: 나는 work — 취재 노트를 정리했다"]

    a = build(aria, working, WORLD, trace_id="t-fixed")
    b = build(aria, working, WORLD, trace_id="t-fixed")
    assert a == b  # 순수 함수 (ADR-009 규칙 5)

    # system = 정적 정체성 프리픽스 (캐시 대상), user = 변동 섹션 고정 순서
    assert "김아리" in a.system
    assert a.user.index("## 작업 기억") < a.user.index("## 세계 상황") < a.user.index("## 임무")
    assert "취재 노트" in a.user


def test_context_working_memory_budget_truncates_oldest():
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    # 예산(1200 tokens ≈ 3000 chars)을 초과하는 항목들 — 최신 우선으로 담고 나머지 절단
    entries = [f"tick {i}: " + ("긴 기억 " * 60) for i in range(100, 0, -1)]
    bundle = build(aria, entries, WORLD, trace_id="t")
    assert "tick 100" in bundle.user  # 최신은 유지
    assert "tick 1:" not in bundle.user  # 오래된 것은 절단 (ADR-009 규칙 2)


def test_fallback_action_is_valid_and_personalized():
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    action = fallback_action(aria, tick=42, trace_id="t-1")
    assert not list(Draft202012Validator(ACTION_SCHEMA).iter_errors(action))
    assert action["action_kind"] == "work"  # achievement가 최강 욕구
    assert action["decision_trace"]["tier"] == "cold_rule"
    assert action == fallback_action(aria, tick=42, trace_id="t-1")  # 결정적
