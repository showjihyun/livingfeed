"""Life Arc 저장 — 액터의 인생 단계와 이번 시즌 방향 (ADR-013, docs/plan/08).

Working Memory(구르는 버퍼)와 달리 지속 상태다: Director가 저빈도로 정하고, 다음
아크 계획까지 남아 매 결정에 배경 프레임을 준다. 지각이 아니라 컨텍스트다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from redis.asyncio import Redis

#: 아크는 인생 단계 규모라 길게 산다 — 다음 계획이 덮어쓴다
DEFAULT_TTL = timedelta(days=30)


@dataclass(frozen=True)
class Arc:
    stage: str
    intention: str


def _decode(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class ArcStore:
    def __init__(self, redis: Redis, *, ttl: timedelta = DEFAULT_TTL) -> None:
        self._redis = redis
        self._ttl = ttl

    @staticmethod
    def _key(world_id: str, actor_id: str) -> str:
        return f"lf:arc:{world_id}:{actor_id}"

    async def set(self, world_id: str, actor_id: str, stage: str, intention: str) -> None:
        key = self._key(world_id, actor_id)
        pipe = self._redis.pipeline()
        pipe.hset(key, mapping={"stage": stage, "intention": intention})
        pipe.expire(key, self._ttl)
        await pipe.execute()

    async def get(self, world_id: str, actor_id: str) -> Arc | None:
        raw = await self._redis.hgetall(self._key(world_id, actor_id))
        if not raw:
            return None
        data = {_decode(k): _decode(v) for k, v in raw.items()}
        if "stage" not in data or "intention" not in data:
            return None
        return Arc(stage=data["stage"], intention=data["intention"])
