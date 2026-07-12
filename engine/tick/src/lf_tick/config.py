"""tick engine 설정 — 환경변수에서 조립한다.

배속·tick 길이는 세계별 설정이나 시즌 중 변경 금지 (ADR-011 §시간 모델).
Phase 1은 env 기반, 세계 엔티티가 생기면 그쪽이 SoT가 된다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

from lf_tick.clock import REAL_SECONDS_PER_TICK_DEFAULT, WORLD_SECONDS_PER_TICK_DEFAULT

#: Phase 1 기본 세계 — FE 목업의 '3월의 세계'와 정합 (docs/design-handoff)
GENESIS_DEFAULT = "2026-03-01T00:00:00+00:00"


@dataclass(frozen=True)
class TickConfig:
    pg_dsn: str
    world_id: str
    genesis: datetime
    real_seconds_per_tick: float = float(REAL_SECONDS_PER_TICK_DEFAULT)
    world_seconds_per_tick: int = WORLD_SECONDS_PER_TICK_DEFAULT
    standby_retry_s: float = 5.0

    @classmethod
    def from_env(cls) -> TickConfig:
        genesis = datetime.fromisoformat(os.environ.get("LF_GENESIS", GENESIS_DEFAULT))
        if genesis.tzinfo is None:
            genesis = genesis.replace(tzinfo=UTC)
        return cls(
            pg_dsn=os.environ.get(
                "LF_PG_DSN", "postgresql://livingfeed:livingfeed@localhost:5432/livingfeed"
            ),
            world_id=os.environ.get("LF_WORLD_ID", "w_main"),
            genesis=genesis,
            real_seconds_per_tick=float(
                os.environ.get("LF_REAL_SECONDS_PER_TICK", REAL_SECONDS_PER_TICK_DEFAULT)
            ),
        )
