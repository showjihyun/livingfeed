"""feed-api 설정 — 환경변수에서 조립한다 (dispatcher config와 동일 규약).

랭킹 계수는 설정값이다 (ADR-014 §2단) — 참여 극대화 단일 목표 최적화 금지.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    opensearch_url: str
    redis_url: str
    index: str = "lf-feed-posts"
    #: fan-out-on-read 결과 캐시 TTL (ADR-014 §2단)
    cache_ttl_s: int = 30
    default_limit: int = 20
    max_limit: int = 100
    #: 랭킹 계수 (ADR-014): score = w_drama·drama + w_proximity·관계근접 + w_recency·시간감쇠
    #: 다양성 보정(w_diversity)은 correlation_id collapse(같은 서사 사슬 도배 방지)로 집행된다.
    #: 관계 근접도는 Relationship Engine(ADR-016) 부재로 아직 질의에 반영되지 않는다.
    w_drama: float = 0.4
    w_proximity: float = 0.25
    w_recency: float = 0.2
    #: 시간 감쇠 반감 스케일 (occurred_at gauss decay)
    recency_scale: str = "6h"

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            opensearch_url=os.environ.get("OPENSEARCH_URL", "http://localhost:9200"),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/1"),
        )
