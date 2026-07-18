"""RedisShardBarrier — 샤드 tick 동기화의 전달 채널 (ADR-012 Phase 2).

키 (전부 소모품 — 이벤트 소싱 대상이 아니다, TTL로 스스로 사라진다):
- lf:tickgo:{world}:{shard}   LIST — 리더의 실행 신호(tick 번호). BLPOP 대기.
- lf:tickack:{world}:{tick}   HASH — shard → counts JSON. 리더가 폴링 집계.

신호가 밀리면(팔로워 다운) 리스트에 쌓인다 — 팔로워는 복귀 후 낡은 신호를
버리고 최신만 따른다(그 사이 tick들은 그 샤드의 침묵이었다, 미루기 정신).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

logger = logging.getLogger("lf.actor.shard")

#: 신호·ack 잔재의 수명 — 하루면 어떤 진단에도 충분하다
_TTL = timedelta(hours=24)
#: 리더의 ack 폴링 간격 — tick 예산(60s) 대비 충분히 촘촘하다
_POLL_S = 0.25


class RedisShardBarrier:
    """lf_tick.shard.ShardBarrier의 Redis 구현."""

    def __init__(
        self, redis: Redis, world_id: str, *, own: set[int], num_shards: int
    ) -> None:
        self._redis = redis
        self._world = world_id
        self._own = frozenset(own)
        self._others = frozenset(range(num_shards)) - self._own
        self._num_shards = num_shards

    @property
    def others(self) -> frozenset[int]:
        return self._others

    def _go_key(self, shard: int) -> str:
        return f"lf:tickgo:{self._world}:{shard}"

    def _ack_key(self, tick: int) -> str:
        return f"lf:tickack:{self._world}:{tick}"

    async def signal(self, tick: int) -> None:
        for shard in sorted(self._others):
            key = self._go_key(shard)
            await self._redis.rpush(key, str(tick))
            await self._redis.expire(key, _TTL)

    async def wait_acks(
        self, tick: int, *, timeout_s: float
    ) -> dict[int, dict[str, Any]]:
        deadline = asyncio.get_running_loop().time() + timeout_s
        key = self._ack_key(tick)
        acks: dict[int, dict[str, Any]] = {}
        while True:
            raw = await self._redis.hgetall(key)
            acks = {
                int(k.decode() if isinstance(k, bytes) else k): json.loads(v)
                for k, v in raw.items()
            }
            if self._others <= set(acks):
                break
            if asyncio.get_running_loop().time() >= deadline:
                missing = sorted(self._others - set(acks))
                logger.warning(
                    "tick %d 샤드 결번 %s — 그 tick의 침묵으로 두고 진행한다 (미루기 정신)",
                    tick, missing,
                )
                break
            await asyncio.sleep(_POLL_S)
        return {s: c for s, c in acks.items() if s in self._others}

    async def next_signal(self, *, timeout_s: float) -> int | None:
        """자기 소유 샤드들의 다음 신호 — 밀린 신호는 버리고 최신만 따른다.

        리더는 같은 tick을 소유 샤드 키마다 넣으므로, 전 소유 키를 듣고
        전부 비운 뒤 최신 tick 하나로 합치면 정합하다. BLPOP은 짧은 청크로
        돈다 — 클라이언트 소켓 시한(설정 불문)보다 길게 소켓을 잡지 않고,
        소켓 시한 예외는 신호 침묵과 동의어로 관용한다.
        """
        keys = [self._go_key(s) for s in sorted(self._own)]
        deadline = asyncio.get_running_loop().time() + timeout_s
        result = None
        while result is None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                result = await self._redis.blpop(
                    keys, timeout=max(1, min(5, int(remaining)))
                )
            except RedisTimeoutError:  # 소켓 시한 — 침묵일 뿐, 죽을 일이 아니다
                continue
        _, raw = result
        tick = int(raw.decode() if isinstance(raw, bytes) else raw)
        drained = 0
        for key in keys:  # 다운 중 밀린 신호는 침묵으로 두고 최신만 (미루기 정신)
            while (nxt := await self._redis.lpop(key)) is not None:
                tick = max(tick, int(nxt.decode() if isinstance(nxt, bytes) else nxt))
                drained += 1
        if drained:
            logger.info("밀린 tick 신호 %d건 건너뜀 → 최신 %d부터", drained, tick)
        return tick

    async def ack(self, tick: int, counts: dict[str, Any]) -> None:
        key = self._ack_key(tick)
        for shard in sorted(self._own):
            await self._redis.hset(key, str(shard), json.dumps(counts))
        await self._redis.expire(key, _TTL)
