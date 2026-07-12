"""AI Runtime 클라이언트 — NATS request-reply (ADR-018).

wire 계약(JSON 형태)은 services/ai-runtime/src/lf_ai_runtime/model.py가 소유한다.
엔진은 SDK를 직접 쓰지 않는다 — 이 클라이언트가 유일한 모델 호출 경로다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import nats.errors
from nats.aio.client import Client as NatsClient

from lf_actor.context import Bundle

logger = logging.getLogger("lf.actor.ai_client")

#: DECIDE 응답 예산 — 상호작용 p95 4s(ADR-020)보다 여유 있게, tick 예산(60s) 안에서
DEFAULT_TIMEOUT_S = 10.0


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
