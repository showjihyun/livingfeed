"""여운(resonance) — 강렬했던 플레이어 대화가 후속 포스트로 분출되기까지 (plan/02).

드라마 재생산 경로의 저장소다: 액터가 플레이어 댓글 교환에서 임계 이상의
감정 강도를 겪으면 그 대화의 correlation·요지가 여기 남고, 이후 리듬 모먼트에
원 사슬을 승계한 후속 포스트로 분출된다 — "그 대화가 마음에 남았다"가 서사가
되는 길이다. 소모품이라 이벤트 소싱 대상이 아니다 (OutreachLedger 선례) —
Redis에 살고 TTL로 스스로 바랜다 (여운은 바래는 것).

구조적 과열 가드의 계약 (소셜 루프 과열의 교훈):
- 액터당 동시 여운 1개 — 더 강한(같으면 더 새로운) 여운만 자리를 대체한다.
- correlation당 1회 소모 — spend()가 마크한 사슬은 재기록을 거부한다 (재분출 없음).
- 분출 쿨다운 장부 — tick 비교가 판정이고 Redis TTL은 안전 상한이라
  워커 재시작에 견고하다 (decay/outreach ledger 선례).
임계·TTL·쿨다운 수치는 params.yaml resonance 절이 원천이다 (하드코딩 금지).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import timedelta

from redis.asyncio import Redis

#: 저장 보존 실시간 상한 — 판정은 tick 비교(ttl_ticks·cooldown_ticks)가 한다
DEFAULT_TTL = timedelta(hours=24)

#: 요지 조각 길이 상한 — 프롬프트 재료·폴백 인용에 충분하고 저장은 가볍게
GIST_MAX = 120


@dataclass(frozen=True)
class Resonance:
    """마음에 남은 대화 한 조각 — 분출 재료의 전부.

    correlation_id가 이 작업의 심장이다: 분출 포스트가 이것을 승계해야
    '이 대화가 낳은 이야기' 배지와 드라마 재생산율 분자가 성립한다.
    player_id는 장부용 기록일 뿐 — 화면 문장에는 절대 싣지 않는다
    (액터는 '그 사람'으로만 말한다, 리시트 정화의 결).
    """

    correlation_id: str
    source_event_id: str
    player_id: str
    comment: str
    reply: str
    intensity: float
    tick: int


class ResonanceStore:
    """액터당 여운 1개 + 소모 마크 + 분출 쿨다운 장부 (Redis)."""

    def __init__(self, redis: Redis, *, ttl: timedelta = DEFAULT_TTL) -> None:
        self._redis = redis
        self._ttl = ttl

    @staticmethod
    def _key(world_id: str, actor_id: str) -> str:
        return f"lf:res:{world_id}:{actor_id}"

    @staticmethod
    def _spent_key(world_id: str, actor_id: str, correlation_id: str) -> str:
        return f"lf:res:spent:{world_id}:{actor_id}:{correlation_id}"

    @staticmethod
    def _burst_key(world_id: str, actor_id: str) -> str:
        return f"lf:res:burst:{world_id}:{actor_id}"

    async def load(self, world_id: str, actor_id: str) -> Resonance | None:
        raw = await self._redis.get(self._key(world_id, actor_id))
        if raw is None:
            return None
        return Resonance(**json.loads(raw))

    async def record(self, world_id: str, actor_id: str, resonance: Resonance) -> bool:
        """여운을 남긴다 — 소모된 사슬은 거부, 자리는 더 강한 것이 차지한다.

        같은 강도면 더 새로운 대화가 자리를 대체한다 (여운은 갱신되는 마음이다).
        반환: 실제로 남았는지 — 호출자의 로그 한 줄용.
        """
        if await self._redis.exists(
            self._spent_key(world_id, actor_id, resonance.correlation_id)
        ):
            return False  # 이미 분출한 사슬 — 재분출 없음 (correlation당 1회)
        current = await self.load(world_id, actor_id)
        if current is not None and current.intensity > resonance.intensity:
            return False  # 액터당 1개 — 지금 마음을 차지한 여운이 더 강하다
        await self._redis.set(
            self._key(world_id, actor_id),
            json.dumps(asdict(resonance), ensure_ascii=False),
            ex=self._ttl,
        )
        return True

    async def clear(self, world_id: str, actor_id: str) -> None:
        """바랜 여운을 지운다 — TTL(tick) 만료 판정은 호출자(phases)의 몫이다."""
        await self._redis.delete(self._key(world_id, actor_id))

    async def spend(
        self, world_id: str, actor_id: str, correlation_id: str, tick: int
    ) -> None:
        """분출 소모 — 여운 삭제 + 사슬 소모 마크 + 쿨다운 장부 기록 (원자적 3종)."""
        pipe = self._redis.pipeline()
        pipe.delete(self._key(world_id, actor_id))
        pipe.set(self._spent_key(world_id, actor_id, correlation_id), "1", ex=self._ttl)
        pipe.set(self._burst_key(world_id, actor_id), str(tick), ex=self._ttl)
        await pipe.execute()

    async def last_burst_tick(self, world_id: str, actor_id: str) -> int | None:
        raw = await self._redis.get(self._burst_key(world_id, actor_id))
        if raw is None:
            return None
        return int(raw.decode() if isinstance(raw, bytes) else raw)
