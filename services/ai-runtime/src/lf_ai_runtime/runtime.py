"""AI Runtime 코어 — task×tier 라우팅 + 구조화 출력 검증 + 예산 집행 (ADR-018).

정책: 응답은 output_schema로 검증한다. 실패 시 1회 수정 재시도,
재실패는 명시적 오류로 반환한다 — 조용한 성공 위장 금지 (ADR-018 §1/4).

라우트 값은 "모델명" 또는 "프로바이더:모델명"이다. 프리픽스가 없으면
기본 프로바이더 소속이다 — 프로바이더·모델 매핑은 전부 설정이다 (ADR-018).

비용·레이트 상한은 BudgetGuard가 집행한다 (budget.py, ADR-018 §3): 호출 전
판정(강등·거절), 호출 후 계량. 가드 없이도 동작한다 — 그때는 상한이 없다.
"""

from __future__ import annotations

import logging

from jsonschema import Draft202012Validator

from lf_ai_runtime.budget import BudgetGuard
from lf_ai_runtime.model import Completion, InferenceRequest, InferenceResponse
from lf_ai_runtime.providers import Provider, ProviderError

logger = logging.getLogger("lf.ai_runtime")

#: 캐논 task×tier 조합 (ADR-018 §인터페이스 표)
TASK_TIERS: tuple[tuple[str, str], ...] = (
    ("decide_action", "hot"),
    ("decide_action", "warm"),
    ("converse", "hot"),
    ("converse", "warm"),
    ("narrate", "system"),
    ("summarize", "system"),
    ("reflect", "warm"),
    ("director_plan", "system"),
)

#: 프로바이더별 tier 기본 모델 — 설정(LF_MODEL_ROUTES)으로 재정의 가능.
DEFAULT_TIER_MODELS: dict[str, dict[str, str]] = {
    # rule 프로바이더는 모델을 무시한다 — 라우팅 표 일관성을 위해 anthropic 표를 공유
    "rule": {"hot": "claude-opus-4-8", "warm": "claude-haiku-4-5", "system": "claude-haiku-4-5"},
    "anthropic": {
        "hot": "claude-opus-4-8",
        "warm": "claude-haiku-4-5",
        "system": "claude-haiku-4-5",
    },
    "openai": {"hot": "gpt-5", "warm": "gpt-5-mini", "system": "gpt-5-mini"},
    "gemini": {"hot": "gemini-2.5-pro", "warm": "gemini-2.5-flash", "system": "gemini-2.5-flash"},
    "deepseek": {"hot": "deepseek-chat", "warm": "deepseek-chat", "system": "deepseek-chat"},
    "glm": {"hot": "glm-4.6", "warm": "glm-4-flash", "system": "glm-4-flash"},
    # 로컬(Ollama/LM Studio): 12GB VRAM은 상주 모델 1개가 현실적 —
    # 티어를 나누면 모델 스왑(언로드/로드)이 tick 예산을 잡아먹는다. 전 티어 단일 모델.
    # qwen3:8b(Q4 ≈ 5GB)는 12GB에 여유롭고 한국어·JSON에 강하다 (thinking 하이브리드 — 지연 주의).
    "local": {"hot": "qwen3:8b", "warm": "qwen3:8b", "system": "qwen3:8b"},
}


#: 라우트 프리픽스로 인정되는 프로바이더 이름 — 모델명 자체의 콜론(예: Ollama의
#: "qwen2.5:14b")과 구분하기 위해, 알려진 이름일 때만 프리픽스로 해석한다.
KNOWN_PROVIDERS = frozenset({"rule", "local", "anthropic", "openai", "gemini", "deepseek", "glm"})


def build_default_routes(provider: str) -> dict[tuple[str, str], str]:
    """기본 프로바이더의 tier 모델로 캐논 라우팅 표를 만든다."""
    tier_models = DEFAULT_TIER_MODELS.get(provider)
    if tier_models is None:
        raise ValueError(f"알 수 없는 프로바이더: {provider}")
    routes = {(task, tier): tier_models[tier] for task, tier in TASK_TIERS}
    if provider in ("anthropic", "rule"):
        routes[("reflect", "warm")] = "claude-sonnet-5"  # 기억 품질 — 중형 (ADR-018 표)
    return routes


#: 하위 호환 별칭 — anthropic 기본 표
DEFAULT_ROUTES: dict[tuple[str, str], str] = build_default_routes("anthropic")


def _validation_errors(output: dict, schema: dict) -> list[str]:
    return [
        f"{'/'.join(map(str, err.absolute_path)) or '(root)'}: {err.message}"
        for err in Draft202012Validator(schema).iter_errors(output)
    ]


class AiRuntime:
    def __init__(
        self,
        provider: Provider | None = None,
        routes: dict[tuple[str, str], str] | None = None,
        *,
        providers: dict[str, Provider] | None = None,
        default_provider: str | None = None,
        guard: BudgetGuard | None = None,
        world_id: str = "w_main",
    ) -> None:
        if provider is not None:
            providers = {provider.name: provider, **(providers or {})}
            default_provider = default_provider or provider.name
        if not providers or default_provider is None:
            raise ValueError("providers와 default_provider가 필요하다")
        if default_provider not in providers:
            raise ValueError(f"기본 프로바이더 '{default_provider}'가 등록되지 않았다")
        self._providers = providers
        self._default = default_provider
        self._routes = DEFAULT_ROUTES if routes is None else routes
        self._guard = guard
        #: trace에 world_id가 없을 때의 예산 버킷 — 예산은 세계 단위다 (ADR-020 §2).
        #: 엔진이 trace.world_id를 싣기 시작하면 그 값이 우선한다 (wire 변경 불요).
        self._world_id = world_id

    def _resolve(self, task: str, tier: str) -> tuple[str, str] | None:
        route = self._routes.get((task, tier))
        if route is None:
            return None
        if ":" in route:
            provider_name, _, model = route.partition(":")
            # 모델명 자체의 콜론(qwen2.5:14b 등)과 구분 — 알려진 프로바이더만 프리픽스
            if provider_name in KNOWN_PROVIDERS:
                return provider_name, model
        return self._default, route

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        world_id = str(request.trace.get("world_id") or self._world_id)
        tier, cap = request.actor_tier, None
        if self._guard is not None:
            decision = await self._guard.check(world_id, tier)
            if not decision.allow:
                # 상한 거절은 명시적 오류다 — 액터는 규칙 행동으로 폴백하고
                # params.fallback으로 화면에서 구분된다 (ADR-012/018 §4)
                logger.warning("예산 거절 (trace=%s): %s", request.bundle.trace_id, decision.reason)
                return InferenceResponse(ok=False, error=decision.reason)
            tier, cap = decision.tier, decision.max_output_tokens

        resolved = self._resolve(request.task, tier)
        if resolved is None:
            return InferenceResponse(
                ok=False,
                error=f"라우팅 없음: task={request.task} tier={tier}",
            )
        provider_name, model = resolved
        provider = self._providers.get(provider_name)
        if provider is None:
            return InferenceResponse(
                ok=False, model=model,
                error=f"프로바이더 미구성: {provider_name} — API 키 환경변수를 설정하라",
            )

        try:
            completion = await self._call(provider, provider_name, request, model, world_id, cap)
        except ProviderError as e:
            logger.warning("추론 실패 (trace=%s): %s", request.bundle.trace_id, e)
            return InferenceResponse(ok=False, error=str(e), model=model)

        errors = _validation_errors(completion.output, request.output_schema)
        if errors:
            # 1회 수정 재시도 (ADR-018 §1) — 재시도분 토큰도 계량된다
            logger.info("스키마 위반 — 수정 재시도 (trace=%s): %s", request.bundle.trace_id, errors)
            try:
                completion = await self._call(
                    provider, provider_name, request, model, world_id, cap, repair_errors=errors
                )
            except ProviderError as e:
                return InferenceResponse(ok=False, error=str(e), model=model)
            errors = _validation_errors(completion.output, request.output_schema)
            if errors:
                return InferenceResponse(
                    ok=False, model=model,
                    error="출력 스키마 위반 (재시도 후에도): " + "; ".join(errors),
                )

        return InferenceResponse(ok=True, output=completion.output, model=model)

    async def _call(
        self,
        provider: Provider,
        provider_name: str,
        request: InferenceRequest,
        model: str,
        world_id: str,
        cap: int | None,
        *,
        repair_errors: list[str] | None = None,
    ) -> Completion:
        """프로바이더 호출 + 사용량 계량.

        검증 실패 여부와 무관하게 기록한다 — 토큰은 이미 나갔다. 예외 경로(타임아웃
        등)는 usage를 알 수 없어 기록하지 못하므로, 지출이 실제보다 과소 집계될 수
        있다(상한은 그만큼 늦게 걸린다).
        """
        completion = await provider.complete(
            request, model, repair_errors=repair_errors, max_output_tokens=cap
        )
        if self._guard is not None:
            await self._guard.record(world_id, provider_name, model, completion.usage)
        return completion
