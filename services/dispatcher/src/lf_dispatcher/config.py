"""dispatcher 설정 — 환경변수에서 조립한다 (ADR-019 §Compose/K8s 이중 정의 드리프트 완화)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class Config:
    pg_dsn: str
    nats_url: str
    env: str
    batch_size: int = 500
    #: LISTEN 유실 대비 폴링 폴백 주기 (ADR-005 §outbox relay 지연)
    poll_interval_s: float = 1.0
    #: 리더가 아닐 때 재시도 간격
    standby_retry_s: float = 3.0
    #: 발행 완료 행 보존 기간 — 이후 purge (디버깅 창)
    purge_keep: timedelta = timedelta(hours=24)
    purge_interval_s: float = 60.0

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            pg_dsn=os.environ.get(
                "LF_PG_DSN", "postgresql://livingfeed:livingfeed@localhost:5432/livingfeed"
            ),
            nats_url=os.environ.get("NATS_URL", "nats://localhost:4222"),
            env=os.environ.get("LF_ENV", "dev"),
        )
