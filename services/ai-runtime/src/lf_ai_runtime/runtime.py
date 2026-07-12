"""AI Runtime 코어 — task×tier 라우팅 + 구조화 출력 검증 (ADR-018).

정책: 응답은 output_schema로 검증한다. 실패 시 1회 수정 재시도,
재실패는 명시적 오류로 반환한다 — 조용한 성공 위장 금지 (ADR-018 §1/4).
"""

from __future__ import annotations

import logging

from jsonschema import Draft202012Validator

from lf_ai_runtime.model import InferenceRequest, InferenceResponse
from lf_ai_runtime.providers import Provider, ProviderError

logger = logging.getLogger("lf.ai_runtime")

#: task × tier → 모델 (ADR-018 §인터페이스 표). 설정으로 재정의 가능 — 코드 변경 없이 교체.
DEFAULT_ROUTES: dict[tuple[str, str], str] = {
    ("decide_action", "hot"): "claude-opus-4-8",
    ("decide_action", "warm"): "claude-haiku-4-5",
    ("converse", "hot"): "claude-opus-4-8",
    ("converse", "warm"): "claude-haiku-4-5",
    ("narrate", "system"): "claude-haiku-4-5",
    ("summarize", "system"): "claude-haiku-4-5",
    ("reflect", "warm"): "claude-sonnet-5",
    ("director_plan", "system"): "claude-opus-4-8",
}


def _validation_errors(output: dict, schema: dict) -> list[str]:
    return [
        f"{'/'.join(map(str, err.absolute_path)) or '(root)'}: {err.message}"
        for err in Draft202012Validator(schema).iter_errors(output)
    ]


class AiRuntime:
    def __init__(
        self,
        provider: Provider,
        routes: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self._provider = provider
        self._routes = DEFAULT_ROUTES if routes is None else routes

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        model = self._routes.get((request.task, request.actor_tier))
        if model is None:
            return InferenceResponse(
                ok=False,
                error=f"라우팅 없음: task={request.task} tier={request.actor_tier}",
            )

        try:
            output = await self._provider.complete(request, model)
        except ProviderError as e:
            logger.warning("추론 실패 (trace=%s): %s", request.bundle.trace_id, e)
            return InferenceResponse(ok=False, error=str(e), model=model)

        errors = _validation_errors(output, request.output_schema)
        if errors:
            # 1회 수정 재시도 (ADR-018 §1)
            logger.info("스키마 위반 — 수정 재시도 (trace=%s): %s", request.bundle.trace_id, errors)
            try:
                output = await self._provider.complete(request, model, repair_errors=errors)
            except ProviderError as e:
                return InferenceResponse(ok=False, error=str(e), model=model)
            errors = _validation_errors(output, request.output_schema)
            if errors:
                return InferenceResponse(
                    ok=False, model=model,
                    error="출력 스키마 위반 (재시도 후에도): " + "; ".join(errors),
                )

        return InferenceResponse(ok=True, output=output, model=model)
