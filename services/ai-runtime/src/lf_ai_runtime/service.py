"""NATS request-reply 서버 — 엔진은 이 subject로만 모델을 호출한다 (ADR-018).

queue group 구독으로 무상태 다중 인스턴스 수평 확장 (ADR-019).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import nats

from lf_ai_runtime.config import Config
from lf_ai_runtime.model import InferenceRequest, InferenceResponse, infer_subject
from lf_ai_runtime.providers import (
    AnthropicProvider,
    OpenAICompatProvider,
    Provider,
    RuleBasedProvider,
)
from lf_ai_runtime.runtime import AiRuntime, build_default_routes

logger = logging.getLogger("lf.ai_runtime.service")

QUEUE_GROUP = "ai-runtime"

#: OpenAI 호환 벤더 스펙: (키 env 후보, 기본 base_url, 토큰 파라미터, 토큰 예산)
#: base_url env(LF_<NAME>_BASE_URL)로 재정의 가능 — GLM 해외 리전(z.ai) 등.
_COMPAT_SPECS: dict[str, tuple[tuple[str, ...], str | None, str, int]] = {
    # gpt-5 계열은 max_tokens를 받지 않고, reasoning 토큰이 예산을 공유한다 → 넉넉히
    "openai": (("OPENAI_API_KEY",), None, "max_completion_tokens", 4096),
    "gemini": (
        ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "max_tokens", 2048,
    ),
    "deepseek": (("DEEPSEEK_API_KEY",), "https://api.deepseek.com", "max_tokens", 2048),
    "glm": (
        ("GLM_API_KEY", "ZHIPU_API_KEY"),
        "https://open.bigmodel.cn/api/paas/v4", "max_tokens", 2048,
    ),
}


#: 로컬 서버(Ollama 기본 포트) — LM Studio는 LF_LOCAL_BASE_URL=http://localhost:1234/v1
LOCAL_BASE_URL_DEFAULT = "http://localhost:11434/v1"


def make_providers(cfg: Config) -> dict[str, Provider]:
    """키가 존재하는 프로바이더를 전부 등록한다. rule/local은 항상 사용 가능."""
    providers: dict[str, Provider] = {"rule": RuleBasedProvider()}
    # local: 키가 필요 없다 (OpenAI SDK가 빈 키를 거부해 자리표시자 사용).
    # 서버 미기동이면 호출 시점에 명시적 연결 오류 → 액터는 규칙 폴백 (ADR-012)
    providers["local"] = OpenAICompatProvider(
        "local",
        api_key=os.environ.get("LF_LOCAL_API_KEY", "local"),
        base_url=os.environ.get("LF_LOCAL_BASE_URL", LOCAL_BASE_URL_DEFAULT),
        max_tokens=1024,
        # qwen3 계열의 thinking을 기본으로 끈다(지연 6배) — LF_LOCAL_THINK=1로 켠다
        no_think=os.environ.get("LF_LOCAL_THINK", "0") != "1",
    )
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        providers["anthropic"] = AnthropicProvider()
    for name, (key_envs, base_url, token_param, max_tokens) in _COMPAT_SPECS.items():
        api_key = next((os.environ[e] for e in key_envs if os.environ.get(e)), None)
        if api_key is None:
            continue
        providers[name] = OpenAICompatProvider(
            name,
            api_key,
            base_url=os.environ.get(f"LF_{name.upper()}_BASE_URL", base_url),
            token_param=token_param,
            max_tokens=max_tokens,
            # gpt-5/o 계열의 reasoning 지연 통제 (해당 모델에만 적용됨)
            reasoning_effort=os.environ.get("LF_OPENAI_REASONING_EFFORT", "low")
            if name == "openai"
            else None,
        )
    return providers


async def serve(cfg: Config, *, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    providers = make_providers(cfg)
    if cfg.provider not in providers:
        raise RuntimeError(
            f"기본 프로바이더 '{cfg.provider}'가 구성되지 않았다 — API 키 환경변수를 확인하라"
        )
    runtime = AiRuntime(
        providers=providers,
        default_provider=cfg.provider,
        routes=cfg.routes or build_default_routes(cfg.provider),
    )

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
            "ai-runtime 대기 — subject=%s 기본=%s 등록=%s",
            subject, cfg.provider, ",".join(sorted(providers)),
        )
        await stop.wait()
    finally:
        await nc.drain()
