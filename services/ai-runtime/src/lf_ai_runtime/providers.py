"""모델 프로바이더 — SDK 사용은 이 모듈에만 존재한다 (ADR-018: 엔진의 SDK 직접 사용 금지).

RuleBasedProvider: 결정적 스텁 — dev/CI 기본값. LLM 비용·키 없이 세계가 돈다.
AnthropicProvider: 실제 LLM. 구조화 출력(output_config.format)으로 스키마를 강제한다.
"""

from __future__ import annotations

import json
import zlib
from typing import Any, Protocol

from lf_ai_runtime.model import InferenceRequest


class ProviderError(Exception):
    """프로바이더 호출 실패 — AI Runtime이 폴백/오류 반환을 결정한다 (ADR-018 §4)."""


class Provider(Protocol):
    name: str

    async def complete(
        self,
        request: InferenceRequest,
        model: str,
        *,
        repair_errors: list[str] | None = None,
    ) -> dict[str, Any]:
        """스키마 검증 전의 구조화 출력 후보를 반환한다."""
        ...


#: 규칙 프로바이더의 일과 행동 풀 — actor.action.performed의 action_kind 어휘와 정합
_ROUTINE_ACTIONS = ("work", "rest", "observe", "move", "reflect_quietly")


class RuleBasedProvider:
    """결정적 규칙 행동 생성기.

    같은 (actor_id, tick) → 같은 행동. LLM 없는 환경(dev/CI)과
    ADR-012 폴백 경로의 '규칙 기반 기본 행동'을 겸한다.
    decide_action 외 task는 Phase 1 범위 밖이다.
    """

    name = "rule"

    async def complete(
        self,
        request: InferenceRequest,
        model: str,
        *,
        repair_errors: list[str] | None = None,
    ) -> dict[str, Any]:
        if request.task != "decide_action":
            raise ProviderError(f"RuleBasedProvider는 '{request.task}' task를 지원하지 않는다")
        actor_id = str(request.trace.get("actor_id", "unknown"))
        tick = int(request.trace.get("tick", 0))
        kind = _ROUTINE_ACTIONS[zlib.crc32(f"{actor_id}:{tick}".encode()) % len(_ROUTINE_ACTIONS)]
        return {
            "action_kind": kind,
            "intent": f"{kind} — 일과를 이어간다 (규칙 행동, tick {tick})",
            "target_actor_id": None,
            "location_id": None,
            "params": {},
            "decision_trace": {
                "trace_id": request.bundle.trace_id,
                "tier": "cold_rule",
            },
        }


def _sanitize_schema(schema: Any) -> Any:
    """구조화 출력이 지원하지 않는 제약을 제거한다 (응답 검증은 원본 스키마로 별도 수행).

    미지원: minLength/maxLength/pattern/format, minimum/maximum 등 수치 제약.
    """
    unsupported = {
        "minLength", "maxLength", "pattern", "format",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        "$schema", "$id",
    }
    if isinstance(schema, dict):
        return {k: _sanitize_schema(v) for k, v in schema.items() if k not in unsupported}
    if isinstance(schema, list):
        return [_sanitize_schema(v) for v in schema]
    return schema


class AnthropicProvider:
    """Anthropic Claude 호출 (구조화 출력).

    bundle.system은 프리픽스 캐시 대상이다 (ADR-009 §섹션 순서, ADR-018 §2).
    """

    name = "anthropic"

    def __init__(self, max_tokens: int = 1024) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic()
        self._max_tokens = max_tokens  # 출력 예산 ≤600 tokens (ADR-009) + 여유

    async def complete(
        self,
        request: InferenceRequest,
        model: str,
        *,
        repair_errors: list[str] | None = None,
    ) -> dict[str, Any]:
        user_content = request.bundle.user
        if repair_errors:
            user_content += (
                "\n\n[수정 요청] 직전 응답이 출력 스키마를 위반했다. 위반 사항을 고쳐 "
                "스키마에 맞는 JSON만 다시 출력하라:\n- " + "\n- ".join(repair_errors)
            )
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=self._max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": request.bundle.system,
                        # 액터별 정적 프리픽스 — 캐시 적중률의 기반 (ADR-009/018)
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": _sanitize_schema(request.output_schema),
                    }
                },
            )
        except self._anthropic.APIStatusError as e:
            raise ProviderError(f"Anthropic API 오류 ({e.status_code}): {e.message}") from e
        except self._anthropic.APIConnectionError as e:
            raise ProviderError(f"Anthropic 연결 실패: {e}") from e

        if response.stop_reason == "refusal":
            raise ProviderError("모델이 요청을 거부했다 (stop_reason=refusal)")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise ProviderError(f"텍스트 응답이 없다 (stop_reason={response.stop_reason})")
        try:
            output = json.loads(text)
        except json.JSONDecodeError as e:
            raise ProviderError(f"JSON 파싱 실패: {e}") from e
        if not isinstance(output, dict):
            raise ProviderError("구조화 출력이 객체가 아니다")
        return output
