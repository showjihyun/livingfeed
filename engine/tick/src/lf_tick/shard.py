"""샤드 배정 — ADR-012 Phase 2 (§배치 구조·일관성 규칙 4).

샤드 수(num_shards)는 고정 상수다: actor→샤드 매핑이 불변이라 워커 증감 시
재분배되는 것은 샤드→워커 배정뿐이다. 샤드 수 자체의 변경은 전 액터 재배치를
뜻하므로 Phase 경계에서만, 계획 작업으로 수행한다.

배리어(ShardBarrier)는 리더와 팔로워의 tick 동기화 계약이다 — 구현은
전달 채널(lf_actor의 Redis)이 갖고, 엔진은 프로토콜만 안다. 조율 신호는
이벤트가 아니다: 역사에 남지 않는 소모품이라 이벤트 스토어에 적재하지 않는다.
"""

from __future__ import annotations

import zlib
from typing import Any, Protocol


def shard_of(actor_id: str, num_shards: int) -> int:
    """actor→샤드 — 결정적 모듈로 해싱 (lod.phase_offset과 같은 crc32 계열)."""
    if num_shards <= 1:
        return 0
    return zlib.crc32(f"shard:{actor_id}".encode()) % num_shards


class ShardBarrier(Protocol):
    """리더-팔로워 tick 동기화 계약 (ADR-012 Phase 2).

    리더: signal(tick) → 자기 샤드 실행 → wait_acks(tick) 집계.
    팔로워: next_signal() → 자기 샤드 실행 → ack(tick, counts).
    ack payload는 {"decided": {...}, "emitted": n} — completed 집계 재료다.
    """

    @property
    def others(self) -> frozenset[int]:
        """리더가 기다릴, 자기 소유가 아닌 샤드들."""
        ...

    async def signal(self, tick: int) -> None:
        """타 샤드 전부에게 이 tick의 실행 신호를 보낸다."""
        ...

    async def wait_acks(
        self, tick: int, *, timeout_s: float
    ) -> dict[int, dict[str, Any]]:
        """타 샤드들의 ack를 기다린다 — 시한 내 못 온 샤드는 결번(그 tick의 침묵)."""
        ...

    async def next_signal(self, *, timeout_s: float) -> int | None:
        """팔로워: 다음 tick 신호를 기다린다. 시한 초과면 None (리더십 재시도 창)."""
        ...

    async def ack(self, tick: int, counts: dict[str, Any]) -> None:
        """팔로워: 자기 샤드의 실행 결과를 보고한다."""
        ...
