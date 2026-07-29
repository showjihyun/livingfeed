"""AI Runtime 설정 — 프로바이더·모델 매핑은 환경별 설정이다 (ADR-018 §배치).

기본 프로바이더는 LF_AI_PROVIDER (rule|anthropic|openai|gemini|deepseek|glm).
키가 설정된 프로바이더는 전부 등록되므로, LF_MODEL_ROUTES로 라우트별
"프로바이더:모델" 혼용이 가능하다 (예: hot은 Claude, warm은 DeepSeek).

비용·레이트 한도(limits)는 여기서 오는 값이 **바닥값**이다: 운영 중 조정은
설정 화면(gateway /admin/ai-limits)이 Redis에 쓰고 가드가 그걸 우선한다
(budget.py). Redis가 없으면 이 env 값이 그대로 유효 한도다.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from lf_ai_runtime.budget import AiLimits, limits_from_env
from lf_ai_runtime.model import Sampling
from lf_ai_runtime.runtime import build_default_routes

logger = logging.getLogger("lf.ai_runtime.config")


@dataclass(frozen=True)
class Config:
    nats_url: str
    env: str
    #: 프리픽스 없는 라우트가 속하는 기본 프로바이더
    provider: str
    routes: dict[tuple[str, str], str] = field(default_factory=dict)
    #: 동시 처리 상한 — 요청별 task를 세마포어로 유계한다 (LF_AI_CONCURRENCY).
    #: 기본 4: 샤드 워커의 병렬 호출을 흡수하면서 원격 rate limit·로컬
    #: OLLAMA_NUM_PARALLEL 기본 권장치(4)와 정합하는 보수적 값.
    concurrency: int = 4
    #: 비용·레이트 한도의 바닥값 (Redis 저장본이 우선)
    limits: AiLimits = field(default_factory=AiLimits)
    #: 예산 카운터·한도 문서가 사는 곳. 미가용이면 프로세스 안 카운터로 강등된다
    #: (그때는 상한이 인스턴스별로만 걸린다 — service.py가 경고를 남긴다)
    redis_url: str = "redis://localhost:6379/0"
    #: 예산 버킷의 기본 세계 — 요청 trace에 world_id가 있으면 그 값이 우선한다
    world_id: str = "w_main"
    #: 샘플링 고정 (ADR-021 §2) — 전부 미설정이 기본이며, 그때는 프로바이더에
    #: 아무것도 보내지 않아 현행 동작과 같다. 값을 주면 그 값이 실제로 전송되고
    #: 결정 기록에 그대로 남는다 — 실험이 무엇을 고정했는지가 데이터에 남는다.
    sampling: Sampling = field(default_factory=Sampling)

    @classmethod
    def from_env(cls) -> Config:
        provider = os.environ.get("LF_AI_PROVIDER", "rule")
        concurrency = int(os.environ.get("LF_AI_CONCURRENCY", "4"))
        if concurrency < 1:
            raise ValueError(f"LF_AI_CONCURRENCY는 1 이상이어야 한다: {concurrency}")
        routes = build_default_routes(provider)
        # 로컬 모델 일괄 교체 (전 티어 단일 모델) — 예: LF_LOCAL_MODEL=exaone3.5:7.8b
        local_model = os.environ.get("LF_LOCAL_MODEL")
        if local_model and provider == "local":
            routes = {key: local_model for key in routes}
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
            concurrency=concurrency,
            limits=limits_from_env(),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            world_id=os.environ.get("LF_WORLD_ID", "w_main"),
            sampling=sampling_from_env(),
        )


def sampling_from_env(env_map: dict[str, str] | None = None) -> Sampling:
    """LF_AI_TEMPERATURE / TOP_P / SEED — 미설정은 None(보내지 않음)이다.

    빈 문자열도 미설정으로 본다: compose가 `LF_AI_TEMPERATURE=`처럼 빈 값을
    넘기는 일이 흔한데, 그걸 0.0으로 읽으면 온도를 0으로 박는 셈이 된다.
    """
    source = env_map if env_map is not None else os.environ

    def number(key: str, cast: Any) -> Any:
        raw = source.get(key)
        if raw is None or not str(raw).strip():
            return None
        try:
            return cast(raw)
        except (TypeError, ValueError):
            logger.warning("%s를 읽지 못해 프로바이더 기본값으로 둔다: %r", key, raw)
            return None

    return Sampling(
        temperature=number("LF_AI_TEMPERATURE", float),
        top_p=number("LF_AI_TOP_P", float),
        seed=number("LF_AI_SEED", int),
    )
