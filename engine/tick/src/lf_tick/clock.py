"""tick ↔ 세계시간 변환 — 유일한 구현 (ADR-011 §시간 모델).

Phase 1 기본값: 1 tick = 실시간 60초 = 세계시간 4분 (세계는 실시간의 4배속).
값은 세계별 설정으로 재정의 가능하나 시즌 중 변경은 금지다 (ADR-011).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

REAL_SECONDS_PER_TICK_DEFAULT = 60
WORLD_SECONDS_PER_TICK_DEFAULT = 240


@dataclass(frozen=True)
class TickClock:
    genesis: datetime  # 세계 생성 시점 = tick 0의 세계시간 (tz-aware 필수)
    world_seconds_per_tick: int = WORLD_SECONDS_PER_TICK_DEFAULT
    real_seconds_per_tick: int = REAL_SECONDS_PER_TICK_DEFAULT

    def __post_init__(self) -> None:
        if self.genesis.tzinfo is None:
            raise ValueError("genesis는 timezone-aware여야 한다")
        if self.world_seconds_per_tick <= 0 or self.real_seconds_per_tick <= 0:
            raise ValueError("tick 길이는 양수여야 한다")

    def world_time_at(self, tick: int) -> datetime:
        if tick < 0:
            raise ValueError("tick은 음수가 될 수 없다")
        return self.genesis + timedelta(seconds=tick * self.world_seconds_per_tick)

    def tick_at(self, world_time: datetime) -> int:
        delta = (world_time - self.genesis).total_seconds()
        if delta < 0:
            raise ValueError("genesis 이전의 세계시간이다")
        return int(delta // self.world_seconds_per_tick)

    def world_days_elapsed(self, tick: int) -> float:
        return tick * self.world_seconds_per_tick / 86_400
