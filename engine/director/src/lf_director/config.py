"""director 설정 — 환경변수에서 조립한다 (dispatcher config와 동일 규약)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    pg_dsn: str
    nats_url: str
    env: str
    world_id: str
    #: 침체 발화 tick 수/임계 재정의 (스모크·데모·시즌 튜닝용 — 기본은 params.yaml)
    quiet_ticks_override: int | None = None
    quiet_threshold_override: float | None = None
    observe_durable: str = "director-observe"
    sys_durable: str = "director-sys"
    batch_size: int = 128
    fetch_timeout_s: float = 2.0

    @classmethod
    def from_env(cls) -> Config:
        quiet = os.environ.get("LF_DIRECTOR_QUIET_TICKS")
        threshold = os.environ.get("LF_DIRECTOR_QUIET_THRESHOLD")
        return cls(
            pg_dsn=os.environ.get(
                "LF_PG_DSN", "postgresql://livingfeed:livingfeed@localhost:5432/livingfeed"
            ),
            nats_url=os.environ.get("NATS_URL", "nats://localhost:4222"),
            env=os.environ.get("LF_ENV", "dev"),
            world_id=os.environ.get("LF_WORLD_ID", "w_main"),
            quiet_ticks_override=int(quiet) if quiet else None,
            quiet_threshold_override=float(threshold) if threshold else None,
        )
