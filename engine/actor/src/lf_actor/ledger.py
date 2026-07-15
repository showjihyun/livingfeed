"""감쇠 장부 저장 — 액터별 감쇠가 정산된 마지막 tick (ADR-012 §Cold 티어 처리).

Cold 배치의 경과 계산이 이 장부에 달려 있다: in-memory만이면 워커 재시작 때
잠든 액터의 경과가 유실돼 밀린 감쇠가 조용히 사라진다. Redis에 살면 재시작해도
경과가 이어진다. 상태가 아니라 장부라 이벤트 소싱 대상이 아니다 — 소모품이며,
유실 시 해당 액터만 현재 tick부터 다시 셀 뿐 세계는 깨지지 않는다.
"""

from __future__ import annotations

from datetime import timedelta

from redis.asyncio import Redis

#: 장부 보존 — 이 창을 넘겨 잠든 세계는 어차피 경과 정산의 의미가 없다
DEFAULT_TTL = timedelta(days=30)


def _decode(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class DecayLedger:
    def __init__(self, redis: Redis, *, ttl: timedelta = DEFAULT_TTL) -> None:
        self._redis = redis
        self._ttl = ttl

    @staticmethod
    def _key(world_id: str) -> str:
        return f"lf:decay:{world_id}"

    async def load_all(self, world_id: str) -> dict[str, int]:
        """세계의 전체 장부 — 워커 기동 시 1회 수화한다."""
        raw = await self._redis.hgetall(self._key(world_id))
        return {_decode(k): int(_decode(v)) for k, v in raw.items()}

    async def mark(self, world_id: str, entries: dict[str, int]) -> None:
        """정산 기록 — tick당 한 번, 이번 tick에 갱신된 액터들을 일괄로."""
        if not entries:
            return
        key = self._key(world_id)
        pipe = self._redis.pipeline()
        pipe.hset(key, mapping={a: str(t) for a, t in entries.items()})
        pipe.expire(key, self._ttl)
        await pipe.execute()
