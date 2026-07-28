"""AI Runtime 클라이언트 — Director 개입 선택(director_plan) 요청 (ADR-013/018).

wire 계약은 services/ai-runtime/src/lf_ai_runtime/model.py가 소유한다. 엔진은
SDK를 직접 쓰지 않는다 — 이 클라이언트가 유일한 모델 호출 경로다.

실패(응답 없음/오류/무효)는 (None, None) — 호출자가 규칙 decide로 폴백한다.
dev 기본(rule 프로바이더)은 director_plan을 지원하지 않으므로 언제나 규칙 폴백이다
(converse와 동일 선례). 실 LLM(local/anthropic)일 때만 맥락 선택이 활성화된다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import nats.errors
from nats.aio.client import Client as NatsClient

logger = logging.getLogger("lf.director.ai_client")

#: 개입 선택 응답 예산 — 저빈도 호출이라 tick 예산(60s) 안에서 여유 있게
DEFAULT_TIMEOUT_S = 12.0

#: Director는 시스템 티어다 (ADR-018 인터페이스 표: director_plan × system)
TIER = "system"
TASK = "director_plan"


def director_trace_id(world_id: str, tick: int) -> str:
    """개입 선택 호출의 trace_id — (세계, tick)에서 결정적으로 파생된다.

    이벤트의 출처(provenance.trace_id, ADR-021 §1)가 이 값을 참조하므로 단일
    원천이어야 한다. 포맷을 두 곳에서 따로 만들면 감사 체인이 조용히 끊긴다.
    """
    return f"director-{world_id}-{tick}"


class DirectorAiClient:
    def __init__(self, nc: NatsClient, env: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._nc = nc
        self._subject = f"lf.{env}.ai.infer"
        self._timeout = timeout_s

    async def plan_intervention(
        self,
        system: str,
        user: str,
        output_schema: dict[str, Any],
        *,
        world_id: str,
        tick: int,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """개입 선택을 요청한다. 반환: (구조화 출력, 사용 모델). 실패는 (None, None)."""
        trace_id = director_trace_id(world_id, tick)
        request = {
            "task": TASK,
            "bundle": {"system": system, "user": user, "trace_id": trace_id},
            "output_schema": output_schema,
            "actor_tier": TIER,
            "trace": {"trace_id": trace_id, "world_id": world_id, "tick": tick},
        }
        try:
            reply = await self._nc.request(
                self._subject,
                json.dumps(request, ensure_ascii=False).encode(),
                timeout=self._timeout,
            )
        except (nats.errors.NoRespondersError, nats.errors.TimeoutError) as e:
            logger.warning("AI Runtime 응답 없음 (director tick=%d): %s", tick, e)
            return None, None

        response = json.loads(reply.data)
        model = response.get("model")
        if not response.get("ok"):
            logger.info("개입 선택 폴백 (tick=%d): %s", tick, response.get("error"))
            return None, model
        output = response.get("output")
        return (output if isinstance(output, dict) else None), model
