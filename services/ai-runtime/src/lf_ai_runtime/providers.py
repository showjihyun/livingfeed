"""모델 프로바이더 — SDK 사용은 이 모듈에만 존재한다 (ADR-018: 엔진의 SDK 직접 사용 금지).

RuleBasedProvider: 결정적 스텁 — dev/CI 기본값. LLM 비용·키 없이 세계가 돈다.
AnthropicProvider: Claude. 구조화 출력(output_config.format)으로 스키마를 강제한다.
OpenAICompatProvider: OpenAI 호환 API 공용 어댑터 — OpenAI/Gemini/DeepSeek/GLM.
  JSON mode + 프롬프트 내 스키마 지시로 출력하고, 검증은 AI Runtime이 원본
  스키마로 수행한다 (벤더별 구조화 출력 지원 편차를 흡수하는 최소공배수 경로).
"""

from __future__ import annotations

import json
import zlib
from typing import Any, Protocol

from lf_ai_runtime.model import Completion, InferenceRequest, Usage


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
        max_output_tokens: int | None = None,
    ) -> Completion:
        """스키마 검증 전의 구조화 출력 후보 + 이번 호출의 토큰 계량기.

        max_output_tokens는 설정된 응답 토큰 상한이다 (budget.AiLimits) —
        프로바이더 기본 예산과 함께 **더 작은 쪽**이 걸린다.
        """
        ...


#: 규칙 프로바이더의 일과 행동 풀 — kind는 action_kind 어휘와 정합, intent는
#: 사람 문장이다: 피드 본문(compose 본문 = intent 원문)과 기억 요약에 그대로
#: 노출되므로 엔진 내부 표기(kind·tick·'규칙')를 담지 않는다.
_ROUTINE_INTENTS: dict[str, tuple[str, ...]] = {
    "work": (
        "밀린 일을 하나씩 붙잡는다",
        "손에 익은 일부터 차근히 밀고 나간다",
        "끝내지 못한 일이 마음에 걸려 다시 책상 앞에 앉는다",
    ),
    "rest": (
        "잠시 숨을 고르며 하루를 정리한다",
        "무리하지 않기로 하고 몸을 쉰다",
        "조용한 시간을 골라 마음을 내려놓는다",
    ),
    "observe": (
        "주변 사람들의 근황을 가만히 살핀다",
        "오가는 이야기들을 한 발짝 떨어져 지켜본다",
        "누가 어떻게 지내는지 눈여겨본다",
    ),
    "move": (
        "바람도 쐴 겸 자리를 옮긴다",
        "익숙한 길을 따라 걸음을 옮긴다",
        "머리를 비우러 잠시 밖으로 나선다",
    ),
    "reflect_quietly": (
        "요즘의 일들을 혼자 곱씹는다",
        "마음에 남은 장면들을 조용히 되짚는다",
        "생각을 정리할 겸 혼자만의 시간을 가진다",
    ),
}
_ROUTINE_ACTIONS = tuple(_ROUTINE_INTENTS)


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
        max_output_tokens: int | None = None,
    ) -> Completion:
        if request.task != "decide_action":
            raise ProviderError(f"RuleBasedProvider는 '{request.task}' task를 지원하지 않는다")
        actor_id = str(request.trace.get("actor_id", "unknown"))
        tick = int(request.trace.get("tick", 0))
        kind = _ROUTINE_ACTIONS[zlib.crc32(f"{actor_id}:{tick}".encode()) % len(_ROUTINE_ACTIONS)]
        phrases = _ROUTINE_INTENTS[kind]
        # 표현 회전은 별도 소금으로 — kind 선택과 상관되면 같은 문장만 돈다
        intent = phrases[zlib.crc32(f"{actor_id}:{tick}:intent".encode()) % len(phrases)]
        # usage는 전부 0 — 규칙 경로에는 토큰도 청구도 없다 (pricing.FREE_PROVIDERS)
        return Completion(
            output={
                "action_kind": kind,
                "intent": intent,
                "target_actor_id": None,
                "location_id": None,
                "params": {},
                "decision_trace": {
                    "trace_id": request.bundle.trace_id,
                    "tier": "cold_rule",
                },
            }
        )


#: 구조화 출력이 지원하지 않는 키워드 — 전송본에서 제거 (응답 검증은 원본 스키마로 별도 수행)
_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minLength", "maxLength", "pattern", "format",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        "$schema", "$id",
    }
)


def _sanitize_schema(schema: Any) -> Any:
    """이벤트 payload 스키마를 구조화 출력 요구사항에 맞게 변환한다.

    - 미지원 제약 키워드 제거 (수치·문자열 제약 등)
    - 모든 object에 additionalProperties: false 강제 (구조화 출력 필수 조건 —
      자유형 object(예: params)는 빈 객체로 좁혀진다; 원본 스키마 검증은 통과)
    - "type": [A, B] 유니언을 anyOf로 변환 (예: ["string", "null"])
    """
    if isinstance(schema, dict):
        out = {k: _sanitize_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED_KEYWORDS}
        type_value = out.get("type")
        if isinstance(type_value, list):
            rest = {k: v for k, v in out.items() if k != "type"}
            return {**rest, "anyOf": [{"type": t} for t in type_value]}
        if type_value == "object" or "properties" in out:
            out.setdefault("properties", {})
            out["additionalProperties"] = False
        return out
    if isinstance(schema, list):
        return [_sanitize_schema(v) for v in schema]
    return schema


def _token_budget(provider_budget: int, cap: int | None) -> int:
    """프로바이더 기본 예산과 설정 상한 중 더 작은 쪽 (상한은 상한이다)."""
    return min(provider_budget, cap) if cap else provider_budget


def _int_attr(obj: Any, name: str) -> int:
    """SDK usage 객체의 정수 필드 — 없거나 None이면 0 (벤더·모델별 편차 흡수)."""
    return int(getattr(obj, name, 0) or 0)


def anthropic_usage(usage: Any) -> Usage:
    """Anthropic usage → Usage. input_tokens는 이미 캐시 밖 잔여분이다."""
    if usage is None:
        return Usage()
    return Usage(
        input_tokens=_int_attr(usage, "input_tokens"),
        output_tokens=_int_attr(usage, "output_tokens"),
        cache_read_tokens=_int_attr(usage, "cache_read_input_tokens"),
        cache_write_tokens=_int_attr(usage, "cache_creation_input_tokens"),
    )


def openai_usage(usage: Any) -> Usage:
    """OpenAI 호환 usage → Usage.

    Anthropic과 달리 prompt_tokens는 **캐시 적중분을 포함한 총량**이다. 캐시분을
    빼내야 단가 계산에서 겹세지 않는다 (캐시 읽기는 입력의 1/10 단가).
    """
    if usage is None:
        return Usage()  # 로컬 서버(Ollama 등)는 usage를 안 줄 수 있다 — 비용 0
    prompt = _int_attr(usage, "prompt_tokens")
    cached = _int_attr(getattr(usage, "prompt_tokens_details", None), "cached_tokens")
    return Usage(
        input_tokens=max(0, prompt - cached),
        output_tokens=_int_attr(usage, "completion_tokens"),
        cache_read_tokens=cached,
    )


class AnthropicProvider:
    """Anthropic Claude 호출 (구조화 출력).

    bundle.system은 프리픽스 캐시 대상이다 (ADR-009 §섹션 순서, ADR-018 §2).
    """

    name = "anthropic"

    def __init__(self, max_tokens: int = 1024) -> None:
        import anthropic

        self._anthropic = anthropic
        # 공유 AsyncAnthropic 하나로 동시 요청을 받는다 — 내부 httpx.AsyncClient는
        # 동시 사용 안전 + 커넥션 풀(max_connections=1000)이라 LF_AI_CONCURRENCY에 여유롭다
        self._client = anthropic.AsyncAnthropic()
        self._max_tokens = max_tokens  # 출력 예산 ≤600 tokens (ADR-009) + 여유

    async def complete(
        self,
        request: InferenceRequest,
        model: str,
        *,
        repair_errors: list[str] | None = None,
        max_output_tokens: int | None = None,
    ) -> Completion:
        user_content = request.bundle.user
        if repair_errors:
            user_content += (
                "\n\n[수정 요청] 직전 응답이 출력 스키마를 위반했다. 위반 사항을 고쳐 "
                "스키마에 맞는 JSON만 다시 출력하라:\n- " + "\n- ".join(repair_errors)
            )
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=_token_budget(self._max_tokens, max_output_tokens),
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
        return Completion(output=output, usage=anthropic_usage(response.usage))


def extract_json_object(text: str) -> dict[str, Any]:
    """모델 응답 텍스트에서 JSON 객체를 꺼낸다 — 코드펜스/잡텍스트 허용."""
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        closing = stripped.rfind("```")
        if first_newline != -1 and closing > first_newline:
            stripped = stripped[first_newline + 1 : closing].strip()
    if not stripped.startswith("{"):
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end <= start:
            raise ProviderError("응답에서 JSON 객체를 찾지 못했다")
        stripped = stripped[start : end + 1]
    try:
        output = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ProviderError(f"JSON 파싱 실패: {e}") from e
    if not isinstance(output, dict):
        raise ProviderError("구조화 출력이 객체가 아니다")
    return output


class OpenAICompatProvider:
    """OpenAI 호환 chat.completions 어댑터 — OpenAI/Gemini/DeepSeek/GLM 공용.

    구조화 출력은 벤더 편차가 커서 최소공배수 경로를 쓴다:
    JSON mode(response_format=json_object) + 프롬프트 내 스키마 지시.
    스키마 검증·수정 재시도는 AI Runtime 공통 정책이 담당한다 (ADR-018 §1).
    """

    def __init__(
        self,
        name: str,
        api_key: str,
        *,
        base_url: str | None = None,
        json_mode: bool = True,
        token_param: str = "max_tokens",
        max_tokens: int = 1024,
        reasoning_effort: str | None = None,
        no_think: bool = False,
    ) -> None:
        import openai

        self.name = name
        self._openai = openai
        # 공유 AsyncOpenAI 하나로 동시 요청을 받는다 (httpx 풀 — Anthropic 쪽 주석 참고).
        # 단 local(Ollama)은 서버가 OLLAMA_NUM_PARALLEL=1이면 직렬화된다 — README 참고.
        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._json_mode = json_mode
        self._token_param = token_param
        self._max_tokens = max_tokens
        self._reasoning_effort = reasoning_effort
        self._no_think = no_think

    async def complete(
        self,
        request: InferenceRequest,
        model: str,
        *,
        repair_errors: list[str] | None = None,
        max_output_tokens: int | None = None,
    ) -> Completion:
        schema_text = json.dumps(request.output_schema, ensure_ascii=False)
        user_content = (
            f"{request.bundle.user}\n\n"
            "## 출력 형식\n"
            "다음 JSON Schema를 정확히 따르는 JSON 객체 하나만 출력하라. "
            "설명·코드펜스·기타 텍스트 금지.\n"
            f"{schema_text}"
        )
        if repair_errors:
            user_content += (
                "\n\n[수정 요청] 직전 응답이 출력 스키마를 위반했다. 위반 사항을 고쳐 "
                "스키마에 맞는 JSON만 다시 출력하라:\n- " + "\n- ".join(repair_errors)
            )
        system_content = request.bundle.system
        # Qwen3 thinking 하이브리드는 기본 ON이라 추론 토큰으로 지연이 ~6배 커진다
        # (Ollama 실측 30s→5s). 구조화 출력·tick 예산을 위해 /no_think 소프트 스위치로
        # 끈다 — qwen3 계열에만 적용(다른 로컬 모델엔 무의미한 토큰이라 건드리지 않는다).
        if self._no_think and model.startswith("qwen3"):
            system_content = f"{system_content} /no_think"
        kwargs: dict[str, Any] = {
            self._token_param: _token_budget(self._max_tokens, max_output_tokens)
        }
        if self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        # reasoning 모델의 지연 통제 — decide류는 깊은 추론이 불필요하다 (tick 예산, ADR-020)
        if self._reasoning_effort and model.startswith(("gpt-5", "o1", "o3", "o4")):
            kwargs["reasoning_effort"] = self._reasoning_effort
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_content},
                ],
                **kwargs,
            )
        except self._openai.APIStatusError as e:
            raise ProviderError(f"{self.name} API 오류 ({e.status_code}): {e.message}") from e
        except self._openai.APIConnectionError as e:
            raise ProviderError(f"{self.name} 연결 실패: {e}") from e

        choice = response.choices[0] if response.choices else None
        text = choice.message.content if choice and choice.message else None
        if not text:
            reason = choice.finish_reason if choice else "no-choice"
            raise ProviderError(f"{self.name}: 텍스트 응답이 없다 (finish_reason={reason})")
        return Completion(
            output=extract_json_object(text),
            usage=openai_usage(getattr(response, "usage", None)),
        )
