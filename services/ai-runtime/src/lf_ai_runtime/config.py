"""AI Runtime 설정 — 프로바이더·모델 매핑은 환경별 설정이다 (ADR-018 §배치)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from lf_ai_runtime.runtime import DEFAULT_ROUTES


@dataclass(frozen=True)
class Config:
    nats_url: str
    env: str
    #: "rule"(기본 — LLM 비용·키 없음) | "anthropic"
    provider: str
    routes: dict[tuple[str, str], str] = field(default_factory=lambda: dict(DEFAULT_ROUTES))

    @classmethod
    def from_env(cls) -> Config:
        routes = dict(DEFAULT_ROUTES)
        # 재정의 형식: {"decide_action/hot": "claude-...", ...}
        raw = os.environ.get("LF_MODEL_ROUTES")
        if raw:
            for key, model in json.loads(raw).items():
                task, _, tier = key.partition("/")
                routes[(task, tier)] = model
        return cls(
            nats_url=os.environ.get("NATS_URL", "nats://localhost:4222"),
            env=os.environ.get("LF_ENV", "dev"),
            provider=os.environ.get("LF_AI_PROVIDER", "rule"),
            routes=routes,
        )
