"""액터 LOD 스케줄링 — 순수 로직 (ADR-011 §액터 LOD 스케줄링).

LOD 정책이 곧 비용 정책이다: LLM 호출량 = f(Hot·Warm 비율).
액터 상태 저장·조회는 Actor Runtime(로드맵 6단계)의 몫이고,
여기는 "이번 tick에 누가 DECIDE 대상인가"의 결정적 계산만 소유한다.
"""

from __future__ import annotations

import zlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

WARM_INTERVAL = 10
COLD_INTERVAL = 100

#: 강등 히스테리시스 — 관심 소멸 후 N tick 동안 티어 유지 (튐 방지, ADR-011)
HOT_DEMOTION_GRACE = 10
WARM_DEMOTION_GRACE = 50


class Tier(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


@dataclass(frozen=True)
class ActorLod:
    tier: Tier
    #: 마지막 관심 신호(플레이어 상호작용, Director 지목, 고중요도 이벤트)의 tick
    last_interest_tick: int


def phase_offset(actor_id: str, interval: int) -> int:
    """액터별 위상 — Warm/Cold DECIDE 를 interval에 걸쳐 결정적으로 분산한다."""
    return zlib.crc32(actor_id.encode()) % interval


def is_due(actor_id: str, lod: ActorLod, tick: int) -> bool:
    """이번 tick에 DECIDE 대상인가 (Hot 매 tick, Warm 10, Cold 100)."""
    match lod.tier:
        case Tier.HOT:
            return True
        case Tier.WARM:
            return tick % WARM_INTERVAL == phase_offset(actor_id, WARM_INTERVAL)
        case Tier.COLD:
            return tick % COLD_INTERVAL == phase_offset(actor_id, COLD_INTERVAL)


def promote(lod: ActorLod, tick: int) -> ActorLod:
    """승격 트리거 → 즉시 Hot (ADR-011)."""
    return ActorLod(tier=Tier.HOT, last_interest_tick=tick)


def touch(lod: ActorLod, tick: int) -> ActorLod:
    """관심 신호 갱신 — 티어는 유지하고 강등 타이머만 리셋한다."""
    return replace(lod, last_interest_tick=tick)


def maybe_demote(lod: ActorLod, tick: int) -> ActorLod:
    """히스테리시스 지나면 한 단계 강등 (Hot→Warm→Cold)."""
    idle = tick - lod.last_interest_tick
    if lod.tier is Tier.HOT and idle >= HOT_DEMOTION_GRACE:
        return replace(lod, tier=Tier.WARM)
    if lod.tier is Tier.WARM and idle >= WARM_DEMOTION_GRACE:
        return replace(lod, tier=Tier.COLD)
    return lod


def due_by_tier(lods: Mapping[str, ActorLod], tick: int) -> dict[Tier, list[str]]:
    """이번 tick의 DECIDE 대상을 티어별로 (actor_id 정렬 — 결정적)."""
    result: dict[Tier, list[str]] = {Tier.HOT: [], Tier.WARM: [], Tier.COLD: []}
    for actor_id in sorted(lods):
        lod = lods[actor_id]
        if is_due(actor_id, lod, tick):
            result[lod.tier].append(actor_id)
    return result


def scheduled_counts(lods: Mapping[str, ActorLod], tick: int) -> dict[str, int]:
    """system.tick.started payload의 scheduled 값 (ADR-011)."""
    due = due_by_tier(lods, tick)
    return {tier.value: len(actors) for tier, actors in due.items()}
