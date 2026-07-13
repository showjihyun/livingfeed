"""AI Runtime 클라이언트 — NATS request-reply (ADR-018).

wire 계약(JSON 형태)은 services/ai-runtime/src/lf_ai_runtime/model.py가 소유한다.
엔진은 SDK를 직접 쓰지 않는다 — 이 클라이언트가 유일한 모델 호출 경로다.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import nats.errors
from nats.aio.client import Client as NatsClient

from lf_actor.context import Bundle

logger = logging.getLogger("lf.actor.ai_client")

#: DECIDE 응답 예산 — 상호작용 p95 4s(ADR-020)보다 여유 있게, tick 예산(60s) 안에서
DEFAULT_TIMEOUT_S = 10.0

_HANGUL = re.compile(r"[가-힣]")
_HAN = re.compile(r"[一-鿿]")


def sanitize_reply(text: str) -> str:
    """소형 로컬 모델이 답변 뒤에 흘리는 사고흐름(주로 한자/중국어)을 자른다.

    한국어 캐주얼 DM은 한자 표의문자를 쓰지 않으므로, 한글과 한자가 섞이면
    첫 한자에서 자른다 — 정상 한국어 답변엔 무해하고, 온전한 비한국어 답변
    (한글이 없는 경우)은 건드리지 않는다.
    """
    if _HANGUL.search(text) and (leak := _HAN.search(text)):
        trimmed = text[: leak.start()].rstrip(" \t\n,·…—-\"'")
        if trimmed:
            return trimmed
    return text

#: 플레이어 응답(converse)의 구조화 출력 — text 한 필드 (표현은 LLM, 형식은 스키마)
REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 600}},
    "required": ["text"],
    "additionalProperties": False,
}


class AiRuntimeClient:
    def __init__(self, nc: NatsClient, env: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._nc = nc
        self._subject = f"lf.{env}.ai.infer"
        self._timeout = timeout_s

    async def decide_action(
        self,
        bundle: Bundle,
        output_schema: dict[str, Any],
        *,
        tier: str,
        actor_id: str,
        tick: int,
    ) -> dict[str, Any] | None:
        """구조화 행동 의도를 요청한다. 실패는 None — 호출자가 규칙 폴백한다 (ADR-012)."""
        request = {
            "task": "decide_action",
            "bundle": {"system": bundle.system, "user": bundle.user, "trace_id": bundle.trace_id},
            "output_schema": output_schema,
            "actor_tier": tier,
            "trace": {"trace_id": bundle.trace_id, "actor_id": actor_id, "tick": tick},
        }
        try:
            reply = await self._nc.request(
                self._subject,
                json.dumps(request, ensure_ascii=False).encode(),
                timeout=self._timeout,
            )
        except (nats.errors.NoRespondersError, nats.errors.TimeoutError) as e:
            logger.warning("AI Runtime 응답 없음 (actor=%s tick=%d): %s", actor_id, tick, e)
            return None

        response = json.loads(reply.data)
        if not response.get("ok"):
            logger.warning(
                "추론 실패 (actor=%s tick=%d): %s", actor_id, tick, response.get("error")
            )
            return None
        output = response.get("output")
        return output if isinstance(output, dict) else None

    async def converse(
        self,
        bundle: Bundle,
        *,
        tier: str,
        actor_id: str,
        tick: int,
    ) -> str | None:
        """플레이어 응답 텍스트를 요청한다. 실패는 None — 호출자가 규칙 답장으로 폴백한다.

        상호작용은 Hot 승격 대상이다 (ADR-011 §개입) — 기본 tier=hot으로 호출된다.
        rule 프로바이더는 converse를 지원하지 않으므로 dev 기본 환경에서는
        언제나 규칙 답장(rules.fallback_reply)이 쓰인다.
        """
        request = {
            "task": "converse",
            "bundle": {"system": bundle.system, "user": bundle.user, "trace_id": bundle.trace_id},
            "output_schema": REPLY_SCHEMA,
            "actor_tier": tier,
            "trace": {"trace_id": bundle.trace_id, "actor_id": actor_id, "tick": tick},
        }
        try:
            reply = await self._nc.request(
                self._subject,
                json.dumps(request, ensure_ascii=False).encode(),
                timeout=self._timeout,
            )
        except (nats.errors.NoRespondersError, nats.errors.TimeoutError) as e:
            logger.warning("AI Runtime 응답 없음 (converse actor=%s): %s", actor_id, e)
            return None

        response = json.loads(reply.data)
        if not response.get("ok"):
            logger.info("converse 폴백 (actor=%s): %s", actor_id, response.get("error"))
            return None
        output = response.get("output")
        text = output.get("text") if isinstance(output, dict) else None
        if not isinstance(text, str) or not text.strip():
            return None
        return sanitize_reply(text)
