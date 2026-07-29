"""tick 파이프라인 단계 계약 (ADR-011 §Tick 파이프라인).

WORLD → PERCEIVE → DECIDE → RESOLVE → CONSOLIDATE.
각 엔진(Actor Runtime, Director, Memory/Emotion/Relationship)은 이 프로토콜의
구현으로 tick에 참여한다. Phase 1 뼈대는 NoopPhases — 로드맵 6단계에서
Actor Runtime이 첫 구현을 연결한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from psycopg import AsyncConnection

ZERO_COUNTS = {"hot": 0, "warm": 0, "cold": 0}


@dataclass(frozen=True)
class TickContext:
    """한 tick 동안 파이프라인 단계들이 공유하는 컨텍스트."""

    world_id: str
    tick: int
    world_time: datetime
    #: 이벤트 적재용 연결 (autocommit) — 단계들은 append()로만 역사를 쓴다 (ADR-002)
    conn: AsyncConnection
    #: 인물이 아는 세계의 이름 — 기본은 world_id와 같다.
    #:
    #: 재생·분기 세계에서만 갈린다 (ADR-021 §4): 역사는 새 world_id에 쌓지만,
    #: 그 세계의 주민은 자기가 원본 세계에 산다고 안다. world_id는 기록용 라벨이지
    #: 인물이 지각하는 것이 아니기 때문이다 — 갈라 두지 않으면 재생된 컨텍스트가
    #: 라벨 하나 때문에 원본과 달라져, 대조가 영원히 어긋난다.
    perceived_world_id: str | None = None

    @property
    def known_world_id(self) -> str:
        """결정 컨텍스트에 실리는 세계 이름 (ADR-009 World 섹션)."""
        return self.perceived_world_id or self.world_id


class TickPhases(Protocol):
    async def schedule(self, ctx: TickContext) -> dict[str, int]:
        """이번 tick의 DECIDE 예정 수(티어별) — system.tick.started payload."""
        ...

    async def world(self, ctx: TickContext) -> None:
        """1. WORLD — 환경 이벤트 적용 (Director 개입 포함, ADR-013)."""
        ...

    async def perceive(self, ctx: TickContext) -> None:
        """2. PERCEIVE — 관측 가능 이벤트 전달 → Working Memory 유입 (ADR-008)."""
        ...

    async def decide(self, ctx: TickContext) -> dict[str, int]:
        """3. DECIDE — 스케줄된 액터 의사결정 (병렬, ADR-012). 반환: 티어별 수행 수."""
        ...

    async def resolve(self, ctx: TickContext) -> int:
        """4. RESOLVE — 충돌 해소(순차·결정적) 후 확정 행동 적재. 반환: 적재 이벤트 수."""
        ...

    async def consolidate(self, ctx: TickContext) -> None:
        """5. CONSOLIDATE — 기억 응고·감정 감쇠·관계 갱신 (ADR-008/015/016)."""
        ...


class NoopPhases:
    """빈 세계 — 액터가 연결되기 전까지의 뼈대 구현 (tick 심장 박동만 남긴다)."""

    async def schedule(self, ctx: TickContext) -> dict[str, int]:
        return dict(ZERO_COUNTS)

    async def world(self, ctx: TickContext) -> None:
        return None

    async def perceive(self, ctx: TickContext) -> None:
        return None

    async def decide(self, ctx: TickContext) -> dict[str, int]:
        return dict(ZERO_COUNTS)

    async def resolve(self, ctx: TickContext) -> int:
        return 0

    async def consolidate(self, ctx: TickContext) -> None:
        return None
