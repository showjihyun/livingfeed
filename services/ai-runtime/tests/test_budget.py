"""비용·레이트 상한 검증 (budget.py·pricing.py — ADR-018 §3, ADR-020 §2).

인프라 없이 돈다: 카운터는 MemoryStore, 시계는 주입한다. 겨누는 것은 네 가지다 —
① 단가 셈이 캐시 토큰을 겹세지 않는다 ② 상한의 80%에서 티어가 강등된다
③ 상한을 넘으면 명시적 오류로 거절된다 ④ 카운터가 죽어도 세계는 계속 돈다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from lf_ai_runtime.budget import (
    AiLimits,
    BudgetGuard,
    MemoryStore,
    calls_key,
    limits_from_env,
    limits_key,
    rpm_key,
    spend_key,
    tokens_key,
    unpriced_key,
)
from lf_ai_runtime.model import Completion, ContextBundle, InferenceRequest, Usage
from lf_ai_runtime.pricing import FALLBACK_PRICE, PriceBook, load_price_overrides
from lf_ai_runtime.providers import anthropic_usage, openai_usage
from lf_ai_runtime.runtime import AiRuntime
from lf_schemas import registry

ACTION_SCHEMA = registry.payload_schema("actor.action.performed")
ENV = "budgettest"
WORLD = "w_main"
FIXED_NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def guard(
    limits: AiLimits | None = None,
    *,
    store: Any = None,
    prices: PriceBook | None = None,
    now: datetime = FIXED_NOW,
) -> BudgetGuard:
    """고정 시계 + 프로세스 안 카운터 가드 — 한도 캐시는 끈다(설정 변경 즉시 반영)."""
    return BudgetGuard(
        ENV,
        store if store is not None else MemoryStore(),
        defaults=limits or AiLimits(),
        prices=prices,
        limits_ttl_s=0.0,
        now_fn=lambda: now,
    )


# ── 키 계약 — gateway ai_limits.py와 **글자 단위로** 같아야 한다 ──────────────


def test_redis_key_contract_is_literal():
    """한쪽만 바꾸면 설정 화면이 집행되지 않는 값을 쓴다 (같은 단정이 gateway에도 있다).

    두 패키지가 서로를 의존하지 않으므로(ADR-018 — SDK는 ai-runtime에만) 계약은
    이 리터럴 단정이 지킨다: 키 형식을 고치면 여기서 먼저 깨진다.
    """
    assert limits_key("dev") == "lf:dev:ai:limits"
    assert spend_key("dev", "w_main", "2026-07-27") == "lf:dev:ai:spend:w_main:2026-07-27"
    assert spend_key("dev", "w_main", "2026-07") == "lf:dev:ai:spend:w_main:2026-07"
    assert calls_key("dev", "w_main", "2026-07-27") == "lf:dev:ai:calls:w_main:2026-07-27"
    assert tokens_key("dev", "w_main", "2026-07-27") == "lf:dev:ai:tokens:w_main:2026-07-27"
    assert rpm_key("dev", "w_main", 29558400) == "lf:dev:ai:rpm:w_main:29558400"
    assert unpriced_key("dev") == "lf:dev:ai:unpriced"


# ── 단가 표 ──────────────────────────────────────────────────────────────────


def test_known_model_price_and_dated_snapshot_prefix():
    book = PriceBook()
    assert book.price("anthropic", "claude-haiku-4-5").input == 1.0
    # 날짜 스냅샷은 최장 프리픽스 일치로 흡수된다
    assert book.price("anthropic", "claude-haiku-4-5-20251001").output == 5.0
    assert book.unpriced == frozenset()


def test_self_hosted_providers_cost_nothing():
    book = PriceBook()
    heavy = Usage(input_tokens=5_000_000, output_tokens=1_000_000)
    assert book.cost_usd("rule", "claude-opus-4-8", heavy) == 0.0
    assert book.cost_usd("local", "qwen3:8b", heavy) == 0.0


def test_unknown_model_uses_conservative_price_and_is_recorded():
    """모르는 모델을 공짜로 셈하면 상한이 조용히 무력해진다 — 보수적으로 센다."""
    book = PriceBook()
    cost = book.cost_usd("openai", "brand-new-model", Usage(input_tokens=1_000_000))
    assert cost == pytest.approx(FALLBACK_PRICE.input)
    assert "brand-new-model" in book.unpriced  # 설정 화면 경고 재료


def test_price_override_from_env_json():
    overrides = load_price_overrides('{"brand-new-model": {"input": 0.5, "output": 2.0}}')
    book = PriceBook(overrides)
    assert book.cost_usd("openai", "brand-new-model", Usage(input_tokens=1_000_000)) == 0.5
    assert book.unpriced == frozenset()  # 재정의로 등재됐다


def test_cost_counts_cache_tokens_at_cache_rates():
    """캐시 읽기는 입력의 1/10 단가 — 프리픽스 캐시가 예산에 반영된다 (ADR-009)."""
    book = PriceBook()
    usage = Usage(input_tokens=1_000_000, cache_read_tokens=1_000_000)
    # opus 입력 $5 + 캐시 읽기 $0.5
    assert book.cost_usd("anthropic", "claude-opus-4-8", usage) == pytest.approx(5.5)


# ── 프로바이더 계량기 ─────────────────────────────────────────────────────────


def test_anthropic_usage_maps_cache_fields():
    class U:
        input_tokens, output_tokens = 100, 20
        cache_read_input_tokens, cache_creation_input_tokens = 900, 50

    usage = anthropic_usage(U())
    assert (usage.input_tokens, usage.cache_read_tokens) == (100, 900)
    assert usage.cache_write_tokens == 50
    assert usage.total_tokens == 1070


def test_openai_usage_excludes_cached_tokens_from_input():
    """OpenAI의 prompt_tokens는 캐시분을 포함한다 — 떼어내지 않으면 겹센다."""

    class U:
        prompt_tokens, completion_tokens = 1000, 40
        prompt_tokens_details = type("D", (), {"cached_tokens": 800})()

    usage = openai_usage(U())
    assert (usage.input_tokens, usage.cache_read_tokens) == (200, 800)
    assert openai_usage(None) == Usage()  # usage 없는 로컬 서버 — 비용 0


# ── 한도 문서 ────────────────────────────────────────────────────────────────


def test_limits_from_json_clamps_hostile_values():
    """한도는 외부(설정 화면·수기 편집)에서 온다 — 집행 경로에 이상값이 새면 안 된다."""
    limits = AiLimits.from_json(
        {"rpm": -5, "daily_usd": -1, "degrade_ratio": 0, "max_output_tokens": -10}
    )
    assert (limits.rpm, limits.daily_usd) == (0, 0.0)
    assert limits.degrade_ratio == 0.1  # 0은 즉시 강등을 뜻해 무의미하다
    assert limits.max_output_tokens == 0
    # 문자열로 와도 읽는다 (env·JSON 왕복), 못 읽으면 base 유지
    assert AiLimits.from_json({"rpm": "30"}).rpm == 30
    assert AiLimits.from_json({"rpm": "nonsense"}).rpm == AiLimits().rpm


def test_unset_default_is_the_cheap_one():
    """미설정 기본값은 dev 안전값이다 — 비용 가드는 싼 쪽으로 실패해야 한다.

    ADR-020 §2의 Phase 1 예산($50/day)은 배포 env가 명시하는 값이다: 기본값이
    비싼 쪽이면 env를 잊은 로컬 실행이 곧 청구서가 된다.
    """
    limits = limits_from_env({})
    assert limits.enabled and limits.daily_usd == 5.0
    # 운영은 env로 ADR-020 값을 올려 잡는다
    assert limits_from_env({"LF_AI_DAILY_USD": "50"}).daily_usd == 50.0
    off = limits_from_env({"LF_AI_LIMITS_ENABLED": "0", "LF_AI_DAILY_USD": "2.5"})
    assert not off.enabled and off.daily_usd == 2.5


# ── 집행: 레이트 ─────────────────────────────────────────────────────────────


async def test_rpm_cap_rejects_beyond_window_allowance():
    g = guard(AiLimits(rpm=2, daily_usd=0))
    assert (await g.check(WORLD, "warm")).allow
    assert (await g.check(WORLD, "warm")).allow
    third = await g.check(WORLD, "warm")
    assert not third.allow
    assert "레이트 리밋" in third.reason


async def test_rpm_zero_means_no_rate_cap():
    g = guard(AiLimits(rpm=0, daily_usd=0))
    for _ in range(50):
        assert (await g.check(WORLD, "hot")).allow


async def test_disabled_limits_pass_everything_through():
    g = guard(AiLimits(enabled=False, rpm=1, daily_usd=0.0001))
    await g.record(WORLD, "anthropic", "claude-opus-4-8", Usage(input_tokens=1_000_000))
    for _ in range(5):
        decision = await g.check(WORLD, "hot")
        assert decision.allow and decision.tier == "hot" and not decision.degraded


# ── 집행: 비용 ───────────────────────────────────────────────────────────────


async def spend(g: BudgetGuard, usd: float) -> None:
    """haiku 단가($1/1M 입력)로 정확히 usd 달러를 태운다."""
    await g.record(
        WORLD, "anthropic", "claude-haiku-4-5", Usage(input_tokens=int(usd * 1_000_000))
    )


async def test_spend_accumulates_into_day_and_month_buckets():
    store = MemoryStore()
    g = guard(AiLimits(daily_usd=10), store=store)
    await spend(g, 1.5)
    assert float(await store.get(spend_key(ENV, WORLD, "2026-07-27"))) == pytest.approx(1.5)
    assert float(await store.get(spend_key(ENV, WORLD, "2026-07"))) == pytest.approx(1.5)


async def test_hot_degrades_to_warm_past_degrade_ratio():
    """80% 도달 — 거절이 아니라 강등이다 (ADR-018 §3, ADR-020 §4)."""
    g = guard(AiLimits(rpm=0, daily_usd=10, degrade_ratio=0.8))
    await spend(g, 8.0)
    decision = await g.check(WORLD, "hot")
    assert decision.allow and decision.degraded and decision.tier == "warm"
    # 이미 소형인 티어는 내릴 곳이 없다 — 강등 없이 통과
    warm = await g.check(WORLD, "warm")
    assert warm.allow and not warm.degraded and warm.tier == "warm"


async def test_daily_cap_rejects_with_explicit_reason():
    g = guard(AiLimits(rpm=0, daily_usd=10))
    await spend(g, 10.0)
    decision = await g.check(WORLD, "hot")
    assert not decision.allow
    assert "일 비용 상한 소진" in decision.reason and "$10.00" in decision.reason


async def test_monthly_cap_rejects_independently_of_daily():
    g = guard(AiLimits(rpm=0, daily_usd=0, monthly_usd=5))
    await spend(g, 5.0)
    decision = await g.check(WORLD, "warm")
    assert not decision.allow and "월 비용 상한 소진" in decision.reason


async def test_exhausted_budget_does_not_consume_a_rate_slot():
    """소진 뒤 거절이 분 창을 잡아먹으면, 상한을 올린 직후에도 계속 막힌다."""
    store = MemoryStore()
    g = guard(AiLimits(rpm=5, daily_usd=1), store=store)
    await spend(g, 1.0)
    for _ in range(20):
        assert not (await g.check(WORLD, "warm")).allow
    g2 = guard(AiLimits(rpm=5, daily_usd=100), store=store)  # 상한을 올렸다
    assert (await g2.check(WORLD, "warm")).allow


async def test_spend_buckets_are_per_world():
    g = guard(AiLimits(rpm=0, daily_usd=10))
    await spend(g, 10.0)
    assert not (await g.check(WORLD, "warm")).allow
    assert (await g.check("w_side", "warm")).allow  # 다른 세계는 자기 예산을 쓴다


async def test_max_output_tokens_rides_on_the_decision():
    g = guard(AiLimits(rpm=0, daily_usd=0, max_output_tokens=256))
    assert (await g.check(WORLD, "hot")).max_output_tokens == 256
    off = guard(AiLimits(rpm=0, daily_usd=0, max_output_tokens=0))
    assert (await off.check(WORLD, "hot")).max_output_tokens is None  # 프로바이더 기본값


# ── 한도 문서를 저장소에서 읽는다 (설정 화면이 쓰는 그 키) ────────────────────


async def test_stored_limits_override_env_defaults():
    store = MemoryStore()
    g = guard(AiLimits(rpm=1000, daily_usd=0), store=store)
    assert (await g.limits()).rpm == 1000
    await store.set(limits_key(ENV), '{"rpm": 1}')
    limits = await g.limits()
    assert limits.rpm == 1  # 저장본이 이긴다
    assert limits.daily_usd == 0  # 빠진 키는 env 바닥값을 잇는다


async def test_corrupt_stored_limits_fall_back_to_env_defaults():
    store = MemoryStore()
    await store.set(limits_key(ENV), "{ not json")
    g = guard(AiLimits(rpm=7), store=store)
    assert (await g.limits()).rpm == 7  # 죽지 않고 바닥값으로 진행


# ── 저장소 장애 — 상한보다 가용성이 앞선다 ──────────────────────────────────


class BrokenStore(MemoryStore):
    async def get(self, key: str):
        raise RuntimeError("redis down")


async def test_counter_failure_allows_the_call_and_warns(caplog):
    """카운터를 못 읽어 추론을 막으면 Redis 장애가 곧 세계 정지가 된다."""
    g = guard(AiLimits(daily_usd=1), store=BrokenStore())
    decision = await g.check(WORLD, "hot")
    assert decision.allow
    assert "상한 일시 무력" in caplog.text


async def test_record_failure_does_not_raise():
    g = guard(store=BrokenStore())
    assert await g.record(WORLD, "anthropic", "claude-haiku-4-5", Usage(1000, 10)) > 0


async def test_snapshot_reports_usage_for_the_settings_screen():
    g = guard(AiLimits(daily_usd=10, rpm=60))
    await spend(g, 2.0)
    snapshot = await g.snapshot(WORLD)
    assert snapshot["available"] is True
    assert snapshot["limits"]["daily_usd"] == 10
    assert snapshot["usage"]["day_usd"] == pytest.approx(2.0)
    assert snapshot["usage"]["calls_today"] == 1
    assert snapshot["usage"]["tokens_today"] == 2_000_000
    assert snapshot["usage"]["unpriced_models"] == []


async def test_snapshot_survives_counter_failure():
    snapshot = await guard(store=BrokenStore()).snapshot(WORLD)
    assert snapshot["available"] is False  # 화면은 서고, 실측 없음이 드러난다


# ── AiRuntime 배선 ───────────────────────────────────────────────────────────


class SpyProvider:
    """유효 응답 + 호출 인자 기록."""

    name = "spy"

    def __init__(self) -> None:
        self.models: list[str] = []
        self.caps: list[int | None] = []

    async def complete(self, request, model, *, repair_errors=None, max_output_tokens=None):
        self.models.append(model)
        self.caps.append(max_output_tokens)
        return Completion(
            output={
                "action_kind": "rest",
                "intent": "숨을 고른다",
                "target_actor_id": None,
                "location_id": None,
                "params": {},
                "decision_trace": {"trace_id": "t-1", "tier": "hot"},
            },
            usage=Usage(input_tokens=1_000_000),
        )


def hot_request(**trace_over) -> InferenceRequest:
    return InferenceRequest(
        task="decide_action",
        bundle=ContextBundle(system="s", user="u", trace_id="t-1"),
        output_schema=ACTION_SCHEMA,
        actor_tier="hot",
        trace={"actor_id": "a_aria_kim", "tick": 3, **trace_over},
    )


async def test_runtime_routes_to_warm_model_when_degraded():
    """강등은 라우팅에 실제로 반영된다 — hot 요청이 warm 모델로 나간다."""
    g = guard(AiLimits(rpm=0, daily_usd=10, degrade_ratio=0.8))
    await spend(g, 8.0)
    provider = SpyProvider()
    runtime = AiRuntime(
        providers={"spy": provider}, default_provider="spy", guard=g, world_id=WORLD
    )
    response = await runtime.infer(hot_request())
    assert response.ok
    assert response.model == "claude-haiku-4-5"  # warm 라우팅 (hot은 opus)


async def test_runtime_rejects_when_budget_exhausted():
    g = guard(AiLimits(rpm=0, daily_usd=1))
    await spend(g, 1.0)
    provider = SpyProvider()
    runtime = AiRuntime(
        providers={"spy": provider}, default_provider="spy", guard=g, world_id=WORLD
    )
    response = await runtime.infer(hot_request())
    assert not response.ok
    assert "일 비용 상한 소진" in response.error
    assert provider.models == []  # 프로바이더를 부르지도 않았다 (지출 0)


async def test_runtime_records_spend_and_passes_output_cap():
    g = guard(AiLimits(rpm=0, daily_usd=100, max_output_tokens=300))
    provider = SpyProvider()
    runtime = AiRuntime(
        providers={"spy": provider}, default_provider="spy", guard=g, world_id=WORLD
    )
    assert (await runtime.infer(hot_request())).ok
    assert provider.caps == [300]  # 설정 상한이 프로바이더까지 내려간다
    snapshot = await g.snapshot(WORLD)
    # spy는 미등재 모델이 아니라 라우팅된 claude-opus-4-8로 셈된다 ($5/1M 입력)
    assert snapshot["usage"]["day_usd"] == pytest.approx(5.0)


async def test_runtime_prefers_world_id_from_trace():
    """예산은 세계 단위다 — trace.world_id가 있으면 그 세계의 버킷을 쓴다."""
    g = guard(AiLimits(rpm=0, daily_usd=100))
    runtime = AiRuntime(
        providers={"spy": SpyProvider()}, default_provider="spy", guard=g, world_id="w_default"
    )
    assert (await runtime.infer(hot_request(world_id="w_other"))).ok
    assert (await g.snapshot("w_other"))["usage"]["calls_today"] == 1
    assert (await g.snapshot("w_default"))["usage"]["calls_today"] == 0


async def test_repair_retry_spend_is_recorded_too():
    """재시도분 토큰도 나갔다 — 안 센 척하면 상한이 실제 청구보다 늦게 걸린다."""

    class Flaky(SpyProvider):
        async def complete(self, request, model, *, repair_errors=None, max_output_tokens=None):
            completion = await super().complete(
                request, model, repair_errors=repair_errors, max_output_tokens=max_output_tokens
            )
            if repair_errors is None:
                return Completion(output={"action_kind": "rest"}, usage=completion.usage)
            return completion

    g = guard(AiLimits(rpm=0, daily_usd=100))
    runtime = AiRuntime(
        providers={"spy": Flaky()}, default_provider="spy", guard=g, world_id=WORLD
    )
    assert (await runtime.infer(hot_request())).ok
    snapshot = await g.snapshot(WORLD)
    assert snapshot["usage"]["calls_today"] == 2  # 위반 응답 + 수정 응답
    assert snapshot["usage"]["day_usd"] == pytest.approx(10.0)


async def test_runtime_without_guard_has_no_limits():
    """가드 미주입 경로는 그대로 동작한다 (기존 배선·테스트 호환)."""
    runtime = AiRuntime(providers={"spy": SpyProvider()}, default_provider="spy")
    assert (await runtime.infer(hot_request())).ok


# ── 액터별 인지 예산 — 세계 상한 안에서의 배분 (ADR-021 §3) ────────────────────


def test_actor_key_contract_is_literal():
    """액터 세그먼트 앞의 'a'가 계약이다 — 없으면 날짜 모양 액터 id가 세계 키와 겹친다."""
    assert spend_key(ENV, WORLD, "2026-07-27", "a_mint") == (
        f"lf:{ENV}:ai:spend:{WORLD}:a:a_mint:2026-07-27"
    )
    assert calls_key(ENV, WORLD, "2026-07-27", "a_mint") == (
        f"lf:{ENV}:ai:calls:{WORLD}:a:a_mint:2026-07-27"
    )
    # actor_id가 없으면 지금까지의 세계 키 그대로다 (기존 카운터와 호환)
    assert spend_key(ENV, WORLD, "2026-07-27") == f"lf:{ENV}:ai:spend:{WORLD}:2026-07-27"


async def test_actor_budget_is_off_by_default():
    """켜는 것이 곧 실험 설정이다 — 끄면 현행 동작과 완전히 같다."""
    g = guard(AiLimits(daily_usd=10.0))
    for _ in range(50):
        await g.record(WORLD, "anthropic", "claude-sonnet-5", Usage(1_000, 1_000),
                       actor_id="a_greedy")
    assert (await g.check(WORLD, "hot", actor_id="a_greedy")).allow


async def test_actor_exhaustion_rejects_while_the_world_still_has_room():
    """한 인물이 제 몫을 다 써도 세계는 계속 돈다 — 그 인물만 규칙으로 산다."""
    store = MemoryStore()
    g = guard(AiLimits(daily_usd=100.0, actor_daily_usd=0.01), store=store)
    for _ in range(5):
        await g.record(WORLD, "anthropic", "claude-sonnet-5", Usage(10_000, 10_000),
                       actor_id="a_spender")

    spent = await g.check(WORLD, "hot", actor_id="a_spender")
    assert not spent.allow
    assert "a_spender" in (spent.reason or "")

    # 다른 인물과 세계 전체는 멀쩡하다 — 배분이지 세계 정지가 아니다
    assert (await g.check(WORLD, "hot", actor_id="a_thrifty")).allow
    assert (await g.check(WORLD, "hot")).allow


async def test_actor_call_cap_rejects_independently_of_cost():
    """비용이 0인 로컬·규칙 프로바이더에서도 '얼마나 자주 생각했나'는 제한된다."""
    store = MemoryStore()
    g = guard(AiLimits(daily_usd=0.0, actor_daily_calls=3), store=store)
    for _ in range(3):
        await g.record(WORLD, "local", "gemma", Usage(10, 10), actor_id="a_chatty")

    rejected = await g.check(WORLD, "hot", actor_id="a_chatty")
    assert not rejected.allow
    assert "호출 상한" in (rejected.reason or "")
    assert (await g.check(WORLD, "hot", actor_id="a_quiet")).allow


async def test_actor_spend_rides_along_with_the_world_counter():
    """액터 몫은 세계 몫과 함께 오른다 — 별도 지갑이 아니라 같은 지출의 분해다."""
    store = MemoryStore()
    g = guard(AiLimits(actor_daily_usd=1.0), store=store)
    cost = await g.record(WORLD, "anthropic", "claude-sonnet-5", Usage(1_000, 1_000),
                          actor_id="a_mint")

    day = "2026-07-27"
    world_spend = float((await store.get(spend_key(ENV, WORLD, day))).decode())
    actor_spend = float((await store.get(spend_key(ENV, WORLD, day, "a_mint"))).decode())
    assert world_spend == pytest.approx(cost)
    assert actor_spend == pytest.approx(cost)


async def test_world_decisions_have_no_actor_bucket():
    """Director의 세계 단위 결정에 액터 지출을 달면 '이 인물이 얼마나 생각했나'가 오염된다."""
    store = MemoryStore()
    g = guard(AiLimits(actor_daily_usd=1.0), store=store)
    await g.record(WORLD, "anthropic", "claude-sonnet-5", Usage(1_000, 1_000))

    day = "2026-07-27"
    assert await store.get(spend_key(ENV, WORLD, day)) is not None
    assert await store.get(spend_key(ENV, WORLD, day, "None")) is None


# ── 샘플링 에코 — 실제로 보낸 값이 기록의 원천이다 (ADR-021 §2) ─────────────────


def test_sampling_defaults_to_sending_nothing():
    """미설정이 기본이다 — 온도를 우리가 박으면 프로바이더 기본값을 덮어써
    세계의 결이 조용히 달라진다. 지정할 때만 고정한다."""
    from lf_ai_runtime.config import sampling_from_env

    assert sampling_from_env({}).sent_kwargs() == {}
    # 빈 문자열도 미설정이다 — compose의 `LF_AI_TEMPERATURE=`를 0.0으로 읽으면 안 된다
    assert sampling_from_env({"LF_AI_TEMPERATURE": ""}).sent_kwargs() == {}
    # 읽지 못하는 값이 가드를 죽이지 않는다
    assert sampling_from_env({"LF_AI_SEED": "abc"}).sent_kwargs() == {}


def test_configured_sampling_is_actually_sent():
    from lf_ai_runtime.config import sampling_from_env

    sampling = sampling_from_env({"LF_AI_TEMPERATURE": "0", "LF_AI_SEED": "42"})
    assert sampling.sent_kwargs() == {"temperature": 0.0, "seed": 42}
    assert sampling.top_p is None  # 주지 않은 것은 여전히 프로바이더 몫


def test_sampling_survives_the_wire():
    """엔진이 받는 것은 JSON이다 — 왕복에서 값이 새면 기록이 비어 버린다."""
    from lf_ai_runtime.model import InferenceResponse, Sampling

    sent = Sampling(temperature=0.0, seed=42, max_output_tokens=600)
    restored = InferenceResponse.from_json(
        InferenceResponse(ok=True, output={}, model="m", sampling=sent).to_json()
    )
    assert restored.sampling == sent


def test_rule_provider_reports_no_sampling():
    """규칙 경로는 부른 모델이 없다 — 샘플링을 지어내면 없는 호출을 있는 것처럼 만든다."""
    from lf_ai_runtime.model import InferenceResponse

    assert InferenceResponse(ok=True, output={}, model=None).to_json()["sampling"] is None
