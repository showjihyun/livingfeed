"""NATS request-reply 서버 — 엔진은 이 subject로만 모델을 호출한다 (ADR-018).

queue group 구독으로 무상태 다중 인스턴스 수평 확장 (ADR-019).
"""

from __future__ import annotations

import asyncio
import json
import logging

import nats

from lf_ai_runtime.config import Config
from lf_ai_runtime.model import InferenceRequest, InferenceResponse, infer_subject
from lf_ai_runtime.providers import AnthropicProvider, Provider, RuleBasedProvider
from lf_ai_runtime.runtime import AiRuntime

logger = logging.getLogger("lf.ai_runtime.service")

QUEUE_GROUP = "ai-runtime"


def make_provider(cfg: Config) -> Provider:
    if cfg.provider == "anthropic":
        return AnthropicProvider()
    if cfg.provider == "rule":
        return RuleBasedProvider()
    raise ValueError(f"알 수 없는 프로바이더: {cfg.provider}")


async def serve(cfg: Config, *, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    runtime = AiRuntime(provider=make_provider(cfg), routes=cfg.routes)

    nc = await nats.connect(cfg.nats_url)
    try:

        async def handle(msg) -> None:
            try:
                request = InferenceRequest.from_json(json.loads(msg.data))
                response = await runtime.infer(request)
            except Exception as e:  # 요청 파싱 실패 등 — 명시적 오류 응답 (조용한 유실 금지)
                logger.exception("요청 처리 실패")
                response = InferenceResponse(ok=False, error=f"요청 처리 실패: {e}")
            await msg.respond(json.dumps(response.to_json(), ensure_ascii=False).encode())

        subject = infer_subject(cfg.env)
        await nc.subscribe(subject, queue=QUEUE_GROUP, cb=handle)
        logger.info(
            "ai-runtime 대기 — subject=%s provider=%s", subject, cfg.provider
        )
        await stop.wait()
    finally:
        await nc.drain()
