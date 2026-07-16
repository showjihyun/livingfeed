"""Redis 타임라인 — fan-out-on-write 개인 피드 (ADR-014 §2단, ADR-003).

팔로우의 두 원천이 공존한다:
- 명시 팔로우 (player.follow.changed — 진짜 팔로우 모델): 플레이어의 선언.
  **철회는 명시적 의사라 이긴다** — 언팔로우한 대상은 관계 엣지가 남아 있어도
  타임라인에 다시 들어오지 않는다 (거부 마커).
- 관계 stand-in (relationship.* — ADR-016): 액터↔플레이어 관계가 생기면
  자동으로 실린다 — "아는 사람의 소식이 내 피드다". 명시 철회 앞에서만 물러난다.

키:
- lf:tl:{world}:{player}     ZSET — member는 피드 아이템 doc(JSON),
  score는 event_id(ULID)의 ms 타임스탬프. ZADD가 재전달을 자연 흡수한다(멱등).
- lf:tlflw:{world}:{actor}   SET — 이 액터의 소식을 받을 플레이어들.
- lf:tlunfl:{world}:{actor}  SET — 명시적으로 언팔로우한 플레이어들 (거부 마커).

한계(수용): 같은 tick에서 first_met과 포스트가 함께 나오면 포스트가 인덱스보다
먼저 도착할 수 있다 — 그 포스트는 실리지 않고 다음 포스트부터 실린다 (최종 일관성).
"""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis

from lf_projector.os_index import envelope_to_doc

#: 플레이어당 타임라인 상한 — 초과분은 fan-out-on-read(OpenSearch)로 폴백 (ADR-014)
TIMELINE_CAP = 500

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(_CROCKFORD)}


def ulid_ms(event_id: str) -> int:
    """ULID 앞 10자(48비트 ms 타임스탬프) 디코드 — 타임라인 score의 좌표계."""
    value = 0
    for char in event_id[:10]:
        value = (value << 5) | _DECODE[char]
    return value


def reply_to_doc(envelope: dict[str, Any]) -> dict[str, Any]:
    """actor.message.sent → Private 등급 피드 아이템 doc.

    6가지 피드는 등급이 다른 같은 데이터다 (ADR-014) — 답장도 포스트와 같은
    모양으로 실려 FE가 한 렌더러로 그린다.
    """
    p = envelope["payload"]
    what = "답장" if p["channel"] == "dm" else "댓글"
    return {
        "event_id": envelope["event_id"],
        "world_id": envelope["world_id"],
        "actor_id": envelope["actor_id"],
        "tick": envelope["tick"],
        "occurred_at": envelope["occurred_at"],
        "causation_id": envelope["causation_id"],
        "correlation_id": envelope["correlation_id"],
        "visibility": "private",
        "title": f"{envelope['actor_id']}의 {what}",
        "body": p["text"],
        "narration_kind": "template",
        "participants": [envelope["actor_id"]],
        "community_id": None,
        "location_id": None,
        "drama_score": 0.0,
        "worthiness": 0.0,
        "source_event_type": envelope["type"],
        "tags": [p["channel"]],
        "media": [],
    }


def follower_pair(payload: dict[str, Any]) -> tuple[str, str] | None:
    """relationship.* payload → (액터, 플레이어). 액터↔플레이어 엣지가 아니면 None."""
    from_id, to_id = payload["from_id"], payload["to_id"]
    from_player, to_player = from_id.startswith("p_"), to_id.startswith("p_")
    if from_player == to_player:  # 액터↔액터(또는 플레이어↔플레이어)는 팔로우가 아니다
        return None
    return (to_id, from_id) if from_player else (from_id, to_id)


class TimelineStore:
    """타임라인 쓰기 경로 — 읽기는 feed-api가 같은 키를 LRANGE 없이 ZSET으로 읽는다."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def timeline_key(world_id: str, player_id: str) -> str:
        return f"lf:tl:{world_id}:{player_id}"

    @staticmethod
    def follower_key(world_id: str, actor_id: str) -> str:
        return f"lf:tlflw:{world_id}:{actor_id}"

    @staticmethod
    def unfollow_key(world_id: str, actor_id: str) -> str:
        return f"lf:tlunfl:{world_id}:{actor_id}"

    async def register_follower(self, world_id: str, actor_id: str, player_id: str) -> None:
        """관계 유래(stand-in) 등록 — 명시적으로 철회한 플레이어는 되살리지 않는다."""
        if await self._redis.sismember(self.unfollow_key(world_id, actor_id), player_id):
            return  # 철회는 명시적 의사다 — 관계가 남아 있어도 존중한다
        await self._redis.sadd(self.follower_key(world_id, actor_id), player_id)

    async def set_follow(
        self, world_id: str, actor_id: str, player_id: str, following: bool
    ) -> None:
        """명시 팔로우 선언/철회 (player.follow.changed) — 마지막 선언이 이긴다."""
        pipe = self._redis.pipeline()
        if following:
            pipe.sadd(self.follower_key(world_id, actor_id), player_id)
            pipe.srem(self.unfollow_key(world_id, actor_id), player_id)
        else:
            pipe.srem(self.follower_key(world_id, actor_id), player_id)
            pipe.sadd(self.unfollow_key(world_id, actor_id), player_id)
        await pipe.execute()

    async def followers(self, world_id: str, actor_id: str) -> set[str]:
        members = await self._redis.smembers(self.follower_key(world_id, actor_id))
        return {m.decode() if isinstance(m, bytes) else m for m in members}

    async def push(self, world_id: str, player_id: str, doc: dict[str, Any]) -> None:
        key = self.timeline_key(world_id, player_id)
        await self._redis.zadd(
            key, {json.dumps(doc, ensure_ascii=False): ulid_ms(doc["event_id"])}
        )
        await self._redis.zremrangebyrank(key, 0, -(TIMELINE_CAP + 1))

    async def fan_out_post(self, envelope: dict[str, Any]) -> int:
        """포스트를 작성자·참여자의 팔로워 타임라인에 싣는다. 반환: 실린 타임라인 수."""
        world_id = envelope["world_id"]
        doc = envelope_to_doc(envelope)
        interested: set[str] = set()
        for actor_id in {envelope["actor_id"], *envelope["payload"]["participants"]}:
            interested |= await self.followers(world_id, actor_id)
        for player_id in sorted(interested):
            await self.push(world_id, player_id, doc)
        return len(interested)

    async def push_reply(self, envelope: dict[str, Any]) -> None:
        """답장/댓글은 팬아웃이 아니라 수신자 단독 배달이다 (Private, ADR-014)."""
        await self.push(
            envelope["world_id"],
            envelope["payload"]["target_player_id"],
            reply_to_doc(envelope),
        )

    async def drop_all(self) -> None:
        """재구축용 파괴 (ADR-003 계약 3) — 타임라인·팔로워 인덱스·거부 마커 전부."""
        async for key in self._redis.scan_iter(match="lf:tl:*"):
            await self._redis.delete(key)
        async for key in self._redis.scan_iter(match="lf:tlflw:*"):
            await self._redis.delete(key)
        async for key in self._redis.scan_iter(match="lf:tlunfl:*"):
            await self._redis.delete(key)
