"""AI Runtime 코어 — task×tier 라우팅 + 구조화 출력 검증 (ADR-018).

정책: 응답은 output_schema로 검증한다. 실패 시 1회 수정 재시도,
재실패는 명시적 오류로 반환한다 — 조용한 성공 위장 금지 (ADR-018 §1/4).

라우트 값은 "모델명" 또는 "프로바이더:모델명"이다. 프리픽스가 없으면
기본 프로바이더 소속이다 — 프로바이더·모델 매핑은 전부 설정이다 (ADR-018).
"""

from __future__ import annotations

import logging

from jsonschema import Draft202012Validator

from lf_ai_runtime.model import InferenceRequest, InferenceResponse
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
}


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

    def _resolve(self, task: str, tier: str) -> tuple[str, str] | None:
        route = self._routes.get((task, tier))
        if route is None:
            return None
        if ":" in route:
            provider_name, _, model = route.partition(":")
            return provider_name, model
        return self._default, route

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        resolved = self._resolve(request.task, request.actor_tier)
        if resolved is None:
            return InferenceResponse(
                ok=False,
                error=f"라우팅 없음: task={request.task} tier={request.actor_tier}",
            )
        provider_name, model = resolved
        provider = self._providers.get(provider_name)
        if provider is None:
            return InferenceResponse(
                ok=False, model=model,
                error=f"프로바이더 미구성: {provider_name} — API 키 환경변수를 설정하라",
            )

        try:
            output = await provider.complete(request, model)
        except ProviderError as e:
            logger.warning("추론 실패 (trace=%s): %s", request.bundle.trace_id, e)
            return InferenceResponse(ok=False, error=str(e), model=model)

        errors = _validation_errors(output, request.output_schema)
        if errors:
            # 1회 수정 재시도 (ADR-018 §1)
            logger.info("스키마 위반 — 수정 재시도 (trace=%s): %s", request.bundle.trace_id, errors)
            try:
                output = await provider.complete(request, model, repair_errors=errors)
            except ProviderError as e:
                return InferenceResponse(ok=False, error=str(e), model=model)
            errors = _validation_errors(output, request.output_schema)
            if errors:
                return InferenceResponse(
                    ok=False, model=model,
                    error="출력 스키마 위반 (재시도 후에도): " + "; ".join(errors),
                )

        return InferenceResponse(ok=True, output=output, model=model)
