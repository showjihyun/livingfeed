"""Working Memory — Memory Fabric의 1계층 (ADR-008 §Working).

현재 tick 주변의 지각·행동 버퍼. Redis 리스트, TTL 수 시간, 최신 우선.
Episodic/Semantic/Relationship 계층은 이후 로드맵 단계에서 붙는다.
"""

from __future__ import annotations

from datetime import timedelta

from redis.asyncio import Redis

DEFAULT_TTL = timedelta(hours=6)
DEFAULT_MAX_ENTRIES = 50


class WorkingMemory:
    def __init__(
        self,
        redis: Redis,
        *,
        ttl: timedelta = DEFAULT_TTL,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._redis = redis
        self._ttl = ttl
        self._max = max_entries

    @staticmethod
    def _key(world_id: str, actor_id: str) -> str:
        return f"lf:wm:{world_id}:{actor_id}"

    async def add(self, world_id: str, actor_id: str, entry: str) -> None:
        key = self._key(world_id, actor_id)
        pipe = self._redis.pipeline()
        pipe.lpush(key, entry)
        pipe.ltrim(key, 0, self._max - 1)
        pipe.expire(key, self._ttl)
        await pipe.execute()

    async def recent(self, world_id: str, actor_id: str, *, limit: int = 20) -> list[str]:
        """최신 우선 항목 목록 (ADR-009 §Working Memory — 최근 우선 절단)."""
        raw = await self._redis.lrange(self._key(world_id, actor_id), 0, limit - 1)
        return [item.decode() if isinstance(item, bytes) else item for item in raw]
