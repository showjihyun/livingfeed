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

관계 이벤트는 인덱스 재료이면서 동시에 "변화 리시트"다 (도파민 §붕괴 방어
"영향력이 안 보임"): 액터→플레이어 마음의 변화를 수치 없는 정성 문장으로
그 플레이어의 Private 타임라인에 싣는다 — receipt_doc / push_receipt.

한계(수용): 같은 tick에서 first_met과 포스트가 함께 나오면 포스트가 인덱스보다
먼저 도착할 수 있다 — 그 포스트는 실리지 않고 다음 포스트부터 실린다 (최종 일관성).
"""

from __future__ import annotations

import json
import re
from typing import Any

from redis.asyncio import Redis

from lf_projector.os_index import envelope_to_doc

#: 플레이어당 타임라인 상한 — 초과분은 fan-out-on-read(OpenSearch)로 폴백 (ADR-014)
TIMELINE_CAP = 500

#: 변화 리시트 생략 임계 — 모든 |delta|가 이보다 작으면 소음으로 보고 배달하지 않는다
#: (도파민 §붕괴 방어 "영향력이 안 보임"의 가시화 vs 스팸 방지의 균형점)
RECEIPT_DELTA_FLOOR = 0.03

#: 지배 차원(|delta| 최대)의 정성 서사 — 수치 노출은 조작감을 만든다 (도파민 §붕괴 방어)
_RECEIPT_SENTENCES: dict[tuple[str, int], str] = {
    ("trust", +1): "신뢰가 조금 자랐다",
    ("trust", -1): "신뢰에 금이 갔다",
    ("intimacy", +1): "마음의 거리가 가까워졌다",
    ("intimacy", -1): "마음의 거리가 멀어졌다",
    ("respect", +1): "존중이 깊어졌다",
    ("respect", -1): "존중이 옅어졌다",
    ("attraction", +1): "설렘이 피어났다",
    ("attraction", -1): "설렘이 사그라들었다",
    ("resentment", +1): "마음 한켠에 앙금이 남았다",
    ("resentment", -1): "앙금이 조금 풀렸다",
}


#: 마일스톤 kind별 서사 라벨 — 모르는 kind는 원문 폴백 (전방 호환)
_MILESTONE_LABELS: dict[str, str] = {
    "first_met": "서로를 알게 됐다",
    "stage_transition": "관계가 새로운 국면으로 넘어갔다",
    "betrayal": "믿음이 배신으로 무너졌다",
    "confession_declined": "고백은 조심스럽게 접혔다",
}


#: note/reason에 실려 오는 내부 표기 → 사람 말 (내레이터 결 — 기계 어휘 비노출)
_INTERACTION_LABELS: dict[str, str] = {
    "player.reaction.added": "좋아요",
    "player.comment.posted": "댓글",
    "player.dm.sent": "DM",
}

_MACHINE_TOKEN = re.compile(r"^[a-z_]+(\.[a-z_]+)+")


def _humanize_note(note: str) -> str:
    """엔진 note의 이벤트 타입 토큰을 사람 말로 — 못 옮기는 기계 표기는 버린다.

    숫자가 든 사유("감정 응고: gratitude 0.69")도 통째로 버린다 — 리시트는
    수치를 노출하지 않는다 (도파민 §붕괴 방어, 조작감 방지). 침묵이 노출보다 낫다.
    """
    match = _MACHINE_TOKEN.match(note)
    if match is not None:
        label = _INTERACTION_LABELS.get(match.group(0))
        if label is None:
            return ""
        note = label + note[match.end():]
    if re.search(r"\d", note):
        return ""
    return note


def _receipt_narration(envelope: dict[str, Any]) -> tuple[str, str] | None:
    """(정성 문장, 엔진이 쓴 사유) — 배달할 만큼의 변화가 아니면 None."""
    p = envelope["payload"]
    if envelope["type"] == "relationship.milestone.reached":
        kind = p["milestone"]
        return _MILESTONE_LABELS.get(kind, kind), _humanize_note(p.get("note", ""))
    dominant, delta = max(p["deltas"].items(), key=lambda item: abs(item[1]))
    if abs(delta) < RECEIPT_DELTA_FLOOR:
        return None  # 미미한 변화는 리시트가 아니라 스팸이다
    return _RECEIPT_SENTENCES[(dominant, 1 if delta > 0 else -1)], _humanize_note(
        p.get("reason", "")
    )


def receipt_doc(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """relationship.* 봉투 → 플레이어 private 리시트 doc. 배달할 게 없으면 None.

    "당신과 관련된 마음의 변화가 세계에 기록됐다"의 가시화 (도파민 §붕괴 방어).
    포스트·답장과 같은 doc 모양이라 FE가 한 렌더러로 그린다 (ADR-014).
    """
    p = envelope["payload"]
    # 액터→플레이어 방향만 — 액터의 마음이 변한 것이 소식이다.
    # 플레이어 자신의 마음(from=플레이어)은 자명하고, 액터↔액터는 세계의 일상이다.
    if p["from_id"].startswith("p_") or not p["to_id"].startswith("p_"):
        return None
    narration = _receipt_narration(envelope)
    if narration is None:
        return None
    sentence, reason = narration
    actor = p["from_id"]
    return {
        "event_id": envelope["event_id"],
        "world_id": envelope["world_id"],
        "actor_id": actor,
        "tick": envelope["tick"],
        "occurred_at": envelope["occurred_at"],
        "causation_id": envelope["causation_id"],
        "correlation_id": envelope["correlation_id"],
        "visibility": "private",
        # 제목에 이름을 넣지 않는다 — 카드 헤더가 디렉터리로 실명을 해석하고,
        # 원시 id는 사람에게 노출하지 않는다 (내레이터 결, 54번 표준)
        "title": "당신과의 사이",
        "body": f"{reason} — {sentence}." if reason else f"{sentence}.",
        "narration_kind": "template",
        "participants": [actor],
        "community_id": None,
        "location_id": None,
        "drama_score": 0.0,
        "worthiness": 0.0,
        "source_event_type": envelope["type"],
        "tags": ["relationship", p["stage"]],
        "created_by": None,
        "media": [],
    }

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
        "created_by": None,
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
        """답장/댓글은 팬아웃이 아니라 수신자 단독 배달이다 (Private, ADR-014).

        액터→액터 댓글(소셜 루프)은 수신 플레이어가 없다 — Private 배달 대상이
        아니므로 조용히 통과한다 (world 가시성 댓글은 gateway 세션 push의 몫).
        """
        target = envelope["payload"]["target_player_id"]
        if not target:
            return
        await self.push(envelope["world_id"], target, reply_to_doc(envelope))

    async def push_receipt(self, envelope: dict[str, Any]) -> None:
        """관계 변화 리시트 — to=플레이어 단독 배달 (Private, 도파민 §붕괴 방어).

        배달 대상이 아니거나(방향·미미한 변화) 문장이 없으면 조용히 통과한다.
        """
        doc = receipt_doc(envelope)
        if doc is None:
            return
        await self.push(envelope["world_id"], envelope["payload"]["to_id"], doc)

    async def drop_all(self) -> None:
        """재구축용 파괴 (ADR-003 계약 3) — 타임라인·팔로워 인덱스·거부 마커 전부."""
        async for key in self._redis.scan_iter(match="lf:tl:*"):
            await self._redis.delete(key)
        async for key in self._redis.scan_iter(match="lf:tlflw:*"):
            await self._redis.delete(key)
        async for key in self._redis.scan_iter(match="lf:tlunfl:*"):
            await self._redis.delete(key)
