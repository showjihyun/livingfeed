"""AI Runtime 코어 검증 — 라우팅, 구조화 출력 검증, 수정 재시도 (ADR-018)."""

from types import SimpleNamespace
from typing import Any

import pytest
from lf_ai_runtime.model import ContextBundle, InferenceRequest
from lf_ai_runtime.providers import (
    OpenAICompatProvider,
    ProviderError,
    RuleBasedProvider,
    _sanitize_schema,
    extract_json_object,
)
from lf_ai_runtime.runtime import AiRuntime, build_default_routes
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


async def test_provider_prefixed_route_to_unconfigured_provider_is_explicit():
    """"provider:model" 라우트 — 미구성 프로바이더는 명시적 오류 (ADR-018 §4)."""
    runtime = AiRuntime(
        providers={"rule": RuleBasedProvider()},
        default_provider="rule",
        routes={("decide_action", "hot"): "gemini:gemini-2.5-pro"},
    )
    response = await runtime.infer(request())
    assert not response.ok
    assert "프로바이더 미구성: gemini" in response.error


async def test_provider_prefixed_route_dispatches_to_named_provider():
    runtime = AiRuntime(
        providers={"rule": RuleBasedProvider(), "other": RuleBasedProvider()},
        default_provider="other",
        routes={("decide_action", "hot"): "rule:whatever-model"},
    )
    response = await runtime.infer(request())
    assert response.ok
    assert response.model == "whatever-model"


async def test_colon_in_model_name_is_not_a_provider_prefix():
    """Ollama식 모델명(qwen2.5:14b)의 콜론은 프리픽스가 아니다."""
    runtime = AiRuntime(
        providers={"rule": RuleBasedProvider()},
        default_provider="rule",
        routes={("decide_action", "hot"): "qwen2.5:14b"},
    )
    response = await runtime.infer(request())
    assert response.ok
    assert response.model == "qwen2.5:14b"  # 통째로 모델명, 기본 프로바이더 소속


async def test_prefixed_local_model_with_tag():
    """"local:qwen2.5:14b" — 첫 콜론만 구분자, 나머지는 모델명."""
    runtime = AiRuntime(
        providers={"rule": RuleBasedProvider(), "local": RuleBasedProvider()},
        default_provider="rule",
        routes={("decide_action", "hot"): "local:qwen2.5:14b"},
    )
    response = await runtime.infer(request())
    assert response.ok
    assert response.model == "qwen2.5:14b"


def test_default_routes_per_provider():
    assert build_default_routes("openai")[("decide_action", "hot")] == "gpt-5"
    assert build_default_routes("deepseek")[("decide_action", "warm")] == "deepseek-chat"
    assert build_default_routes("glm")[("decide_action", "hot")] == "glm-4.6"
    assert build_default_routes("gemini")[("summarize", "system")] == "gemini-2.5-flash"
    # anthropic의 reflect 특례는 유지 (ADR-018 표)
    assert build_default_routes("anthropic")[("reflect", "warm")] == "claude-sonnet-5"
    # local은 전 티어 단일 모델 (12GB VRAM = 상주 1개, 스왑 방지)
    local = build_default_routes("local")
    assert len({model for model in local.values()}) == 1
    with pytest.raises(ValueError):
        build_default_routes("unknown")


def test_local_provider_registered_without_key(monkeypatch):
    from lf_ai_runtime.config import Config
    from lf_ai_runtime.service import make_providers

    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    providers = make_providers(Config(nats_url="", env="t", provider="local"))
    assert "local" in providers and "rule" in providers  # 키 없이도 항상 등록
    assert "openai" not in providers


def test_local_model_env_replaces_all_tiers(monkeypatch):
    from lf_ai_runtime.config import Config

    monkeypatch.setenv("LF_AI_PROVIDER", "local")
    monkeypatch.setenv("LF_LOCAL_MODEL", "exaone3.5:7.8b")
    cfg = Config.from_env()
    assert set(cfg.routes.values()) == {"exaone3.5:7.8b"}


def test_extract_json_object_tolerates_fences_and_prose():
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('결과는 다음과 같다:\n{"a": {"b": 2}} 이상.') == {"a": {"b": 2}}
    with pytest.raises(ProviderError):
        extract_json_object("JSON 없음")


def _fake_openai_provider(no_think: bool) -> tuple[OpenAICompatProvider, dict]:
    """실 API 없이 client를 페이크로 대체 — 전송된 messages를 캡처한다."""
    provider = OpenAICompatProvider("local", api_key="x", no_think=no_think)
    captured: dict[str, Any] = {}

    async def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": 1}'))]
        )

    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    return provider, captured


async def test_no_think_appended_only_for_qwen3():
    # qwen3 계열 + no_think → system 접미에 /no_think (thinking 끄기, 지연 방지)
    provider, captured = _fake_openai_provider(no_think=True)
    await provider.complete(request(), "qwen3:8b")
    system = captured["messages"][0]
    assert system["role"] == "system"
    assert system["content"].endswith("/no_think")


async def test_no_think_skips_non_qwen3_models():
    # 같은 no_think 플래그라도 qwen3가 아니면 무의미한 토큰을 붙이지 않는다
    provider, captured = _fake_openai_provider(no_think=True)
    await provider.complete(request(), "qwen2.5:14b")
    assert "/no_think" not in captured["messages"][0]["content"]


async def test_no_think_disabled_leaves_qwen3_untouched():
    # LF_LOCAL_THINK=1 등으로 thinking을 켜면 스위치를 붙이지 않는다
    provider, captured = _fake_openai_provider(no_think=False)
    await provider.complete(request(), "qwen3:8b")
    assert "/no_think" not in captured["messages"][0]["content"]
    with pytest.raises(ProviderError):
        extract_json_object("[1, 2]")  # 객체가 아니다


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


def test_sanitize_schema_meets_structured_output_requirements():
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

    def check_objects(node) -> None:
        """구조화 출력 필수 조건: 모든 object에 additionalProperties=false, type 유니언 금지."""
        if isinstance(node, dict):
            assert not isinstance(node.get("type"), list), "type 유니언은 anyOf로 변환돼야 한다"
            if node.get("type") == "object" or "properties" in node:
                assert node.get("additionalProperties") is False
            for value in node.values():
                check_objects(value)
        elif isinstance(node, list):
            for value in node:
                check_objects(value)

    check_objects(sanitized)
    # 원본의 자유형 params({"type": "object"})와 nullable 유니언이 실제로 변환됐다
    assert sanitized["properties"]["params"]["additionalProperties"] is False
    assert sanitized["properties"]["decision_trace"]["additionalProperties"] is False
    assert {"type": "null"} in sanitized["properties"]["target_actor_id"]["anyOf"]
    # 원본은 변형되지 않았다 (순수 함수)
    assert ACTION_SCHEMA["properties"]["params"].get("additionalProperties") is None
