"""AI Runtime 코어 검증 — 라우팅, 구조화 출력 검증, 수정 재시도 (ADR-018)."""

from typing import Any

from lf_ai_runtime.model import ContextBundle, InferenceRequest
from lf_ai_runtime.providers import RuleBasedProvider, _sanitize_schema
from lf_ai_runtime.runtime import AiRuntime
from lf_schemas import registry

ACTION_SCHEMA = registry.payload_schema("actor.action.performed")


def request(**overrides) -> InferenceRequest:
    base: dict[str, Any] = dict(
        task="decide_action",
        bundle=ContextBundle(system="당신은 김아리다.", user="행동을 결정하라.", trace_id="t-1"),
        output_schema=ACTION_SCHEMA,
        actor_tier="hot",
        trace={"actor_id": "a_aria_kim", "tick": 3},
    )
    base.update(overrides)
    return InferenceRequest(**base)


async def test_rule_provider_output_is_deterministic_and_valid():
    runtime = AiRuntime(RuleBasedProvider())
    first = await runtime.infer(request())
    second = await runtime.infer(request())
    assert first.ok and second.ok
    assert first.output == second.output  # 같은 (actor, tick) → 같은 행동
    assert first.output["decision_trace"]["tier"] == "cold_rule"
    assert first.model == "claude-opus-4-8"  # hot 라우팅 (ADR-018 표)


async def test_unknown_route_is_explicit_error():
    runtime = AiRuntime(RuleBasedProvider(), routes={})
    response = await runtime.infer(request())
    assert not response.ok
    assert "라우팅 없음" in response.error


class SchemaViolatingProvider:
    """첫 응답은 스키마 위반, 수정 재시도에서 유효 응답 — ADR-018 §1의 1회 재시도 경로."""

    name = "flaky"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request, model, *, repair_errors=None):
        self.calls += 1
        if repair_errors is None:
            return {"action_kind": "speak"}  # required 누락 — 위반
        return {
            "action_kind": "speak",
            "intent": "수정된 응답",
            "target_actor_id": None,
            "location_id": None,
            "params": {},
            "decision_trace": {"trace_id": "t-1", "tier": "hot"},
        }


async def test_schema_violation_triggers_single_repair_retry():
    provider = SchemaViolatingProvider()
    runtime = AiRuntime(provider)
    response = await runtime.infer(request())
    assert response.ok
    assert provider.calls == 2
    assert response.output["intent"] == "수정된 응답"


class AlwaysInvalidProvider:
    name = "broken"

    async def complete(self, request, model, *, repair_errors=None):
        return {"nonsense": True}


async def test_persistent_violation_returns_explicit_error():
    runtime = AiRuntime(AlwaysInvalidProvider())
    response = await runtime.infer(request())
    assert not response.ok
    assert "스키마 위반" in response.error  # 조용한 성공 위장 금지 (ADR-018 §4)


def test_sanitize_schema_strips_unsupported_constraints():
    sanitized = _sanitize_schema(ACTION_SCHEMA)

    def has_key(node, key) -> bool:
        if isinstance(node, dict):
            return key in node or any(has_key(v, key) for v in node.values())
        if isinstance(node, list):
            return any(has_key(v, key) for v in node)
        return False

    assert has_key(ACTION_SCHEMA, "maxLength")  # 원본엔 있고
    assert not has_key(sanitized, "maxLength")  # 전송본엔 없다
    assert has_key(sanitized, "required")  # 구조 제약은 유지
