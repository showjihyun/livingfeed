"""AI Runtime 설정 — 프로바이더·모델 매핑은 환경별 설정이다 (ADR-018 §배치).

기본 프로바이더는 LF_AI_PROVIDER (rule|anthropic|openai|gemini|deepseek|glm).
키가 설정된 프로바이더는 전부 등록되므로, LF_MODEL_ROUTES로 라우트별
"프로바이더:모델" 혼용이 가능하다 (예: hot은 Claude, warm은 DeepSeek).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from lf_ai_runtime.runtime import build_default_routes


@dataclass(frozen=True)
class Config:
    nats_url: str
    env: str
    #: 프리픽스 없는 라우트가 속하는 기본 프로바이더
    provider: str
    routes: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Config:
        provider = os.environ.get("LF_AI_PROVIDER", "rule")
        routes = build_default_routes(provider)
        # 재정의 형식: {"decide_action/hot": "gemini:gemini-2.5-pro", ...}
        raw = os.environ.get("LF_MODEL_ROUTES")
        if raw:
            for key, model in json.loads(raw).items():
                task, _, tier = key.partition("/")
                routes[(task, tier)] = model
        return cls(
            nats_url=os.environ.get("NATS_URL", "nats://localhost:4222"),
            env=os.environ.get("LF_ENV", "dev"),
            provider=provider,
            routes=routes,
        )
