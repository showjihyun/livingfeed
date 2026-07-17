"""도파민 포인트 — 관계가 보상이 되는 순간의 판단·빈도 장부 (plan/02·03).

'기억됨' 선제 DM의 두 재료를 소유한다: 후보 선정(순수 — 임계를 넘은 플레이어
엣지 중 최고 관계 1명, 동률은 id 순 결정적)과 빈도 장부(Redis — (액터,플레이어)당
세계 하루 1회 이하 가드). 장부는 tick 값을 저장하므로 워커 재시작에 견고하다
(decay ledger 선례 — 소모품이며 유실 시 그 쌍만 하루 가드를 다시 셀 뿐이다).
임계·쿨다운 수치는 params.yaml outreach 절이 원천이다 (하드코딩 금지).
"""

from __future__ import annotations

from datetime import timedelta

from lf_relationship import RelationshipState
from redis.asyncio import Redis

#: 플레이어 식별자 접두 — 스키마 player_id 패턴(^p_)의 최소 투영 (특정 인물 아님)
PLAYER_ID_PREFIX = "p_"

#: 장부 보존 — 쿨다운(세계 하루 = 실제 6h)의 안전 상한. 판정은 tick 비교가 한다
DEFAULT_TTL = timedelta(hours=24)


def dm_score(state: RelationshipState) -> float:
    """선제 DM의 관계 온도 — trust+intimacy 합 (plan/02 '신뢰를 얻은 AI')."""
    return float(state.dimensions.get("trust", 0.0)) + float(
        state.dimensions.get("intimacy", 0.0)
    )


def pick_dm_target(
    edges: dict[str, RelationshipState], *, threshold: float
) -> str | None:
    """선제 DM 후보 — 임계 이상 플레이어 엣지 중 최고 관계 1명 (순수·결정적).

    액터 엣지는 후보가 아니다 — 액터 간 사교는 소셜 루프(피드·댓글)의 몫이다.
    동률은 id 순으로 앞선 쪽 — 같은 관계 상태면 언제나 같은 답이다.
    """
    candidates = [
        (other, dm_score(state))
        for other, state in edges.items()
        if other.startswith(PLAYER_ID_PREFIX) and dm_score(state) >= threshold
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: (-c[1], c[0]))[0]


class OutreachLedger:
    """선제 DM 빈도 장부 — (액터,플레이어)별 마지막 발신 tick (Redis).

    상태가 아니라 장부라 이벤트 소싱 대상이 아니다 (decay ledger 선례).
    """

    def __init__(self, redis: Redis, *, ttl: timedelta = DEFAULT_TTL) -> None:
        self._redis = redis
        self._ttl = ttl

    @staticmethod
    def _key(world_id: str, actor_id: str, player_id: str) -> str:
        return f"lf:pdm:{world_id}:{actor_id}:{player_id}"

    async def last_sent_tick(
        self, world_id: str, actor_id: str, player_id: str
    ) -> int | None:
        raw = await self._redis.get(self._key(world_id, actor_id, player_id))
        if raw is None:
            return None
        return int(raw.decode() if isinstance(raw, bytes) else raw)

    async def mark(
        self, world_id: str, actor_id: str, player_id: str, tick: int
    ) -> None:
        await self._redis.set(
            self._key(world_id, actor_id, player_id), str(tick), ex=self._ttl
        )
