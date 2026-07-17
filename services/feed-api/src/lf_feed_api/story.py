"""서사 사슬 조회 — "이 이야기의 시작점" (plan/03 §단계 3→4, ADR-002 인과 체인).

내 개입에서 시작된 사건 연쇄를 correlation_id 하나로 따라간다. 실세의 보상은
권력이 아니라 저자성이다 — origin이 요청자 자신이면 started_by_you로 알린다.

es.events 직접 읽기는 읽기 API 규칙(ADR-003: 프로젝션만 읽는다)의 문서화된
예외다: 인과 사슬은 원본(es)이 곧 정본이고, 사슬 전용 프로젝션은 원본의
복사본에 지나지 않는다 — projector replay/verify가 같은 이유로 es를 읽는 선례.
여기서는 read-only SELECT만 한다. 쓰기는 여전히 lf-eventstore append 단일 경로다.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("lf.feed_api.story")

# ── 무서사 제외 — 블랙리스트 ──────────────────────────────────────────
# 블랙리스트인 이유: 새 이벤트 타입은 기본 포함(타입 라벨 폴백)이다.
# 화이트리스트 누락으로 이야기의 마디가 조용히 사라지는 것보다, 낯선 항목이
# 타입 라벨로 드러나는 쪽이 관찰로 고치기 쉽다 (침묵 실패 회피).

#: 세계의 심장박동 — 매 tick 반복되는 기계 신호라 서사가 아니다
_NON_NARRATIVE_PREFIXES: tuple[str, ...] = ("system.tick.",)

#: 무대 장치 노브 — payload가 수치 조정뿐이라 사람이 읽을 문장이 없다.
#: (system.director.intervened/arc_planned는 reason/intention 문장이 있어 남긴다)
_NON_NARRATIVE_TYPES = frozenset({
    "system.director.feed_boosted",
    "system.director.spotlighted",
    "system.director.season_set",
})


def is_narrative(type_: str) -> bool:
    """타임라인에 실을 이벤트인가 — 위 블랙리스트 근거 참고."""
    if type_.startswith(_NON_NARRATIVE_PREFIXES):
        return False
    return type_ not in _NON_NARRATIVE_TYPES


# ── 타입별 한글 한 줄 요약 ────────────────────────────────────────────

#: 타입 → 사람 문장이 담긴 payload 필드 (headline/intent/text/description/reason 계열)
_SUMMARY_FIELDS: dict[str, str] = {
    "feed.post.published": "title",
    "actor.action.performed": "intent",
    "actor.message.sent": "text",
    "actor.emotion.shifted": "reason",
    "actor.memory.consolidated": "summary",
    "actor.belief.formed": "statement",
    "actor.goal.advanced": "description",
    "actor.goal.achieved": "description",
    "player.comment.posted": "text",
    "player.dm.sent": "text",
    "relationship.state.changed": "reason",
    "relationship.milestone.reached": "note",
    "world.incident.occurred": "description",
    "world.observation.surfaced": "observation",
    "system.director.intervened": "reason",
    "system.director.arc_planned": "intention",
}

#: 반응 종류 → 사람 말 — 내부 표기(kind 코드)를 화면 문장에 내보내지 않는다
_REACTION_LABELS: dict[str, str] = {"like": "좋아요"}


def summarize(type_: str, payload: dict[str, Any]) -> str:
    """이벤트 한 건 → 타임라인 한 줄. 모르는 타입은 타입 라벨 폴백(숨기지 않는다)."""
    if type_ == "player.reaction.added":
        label = _REACTION_LABELS.get(payload.get("kind", ""), "따뜻한 반응")
        return f"{label}로 마음을 보탰다"
    if type_ == "player.follow.changed":
        return (
            "소식을 따라가기로 했다" if payload.get("following") else "따라가기를 거뒀다"
        )
    field = _SUMMARY_FIELDS.get(type_)
    if field and payload.get(field):
        return str(payload[field])
    return type_


# ── 표시 이름 해석 ───────────────────────────────────────────────────


def display_actor(
    actor_id: str | None,
    payload: dict[str, Any],
    *,
    names: dict[str, str],
    requester: str | None,
) -> str:
    """행위 주체의 표시 이름 — 화면 문장에 식별자를 내보내지 않는다.

    - 액터: read.actors 이름, 미상이면 '누군가' (FE 내레이터 규약과 같은 결)
    - 플레이어: 요청자 본인만 '당신', 타인은 '어느 관찰자' (관찰자 익명성)
    - 주체 없는 사건(world.*/director 개입): 세계 자신의 목소리 — '세계'
    """
    if actor_id is not None:
        return names.get(actor_id, "누군가")
    player_id = payload.get("player_id")
    if player_id is not None:
        return "당신" if requester is not None and player_id == requester else "어느 관찰자"
    return "세계"


# ── es 사슬 읽기 ─────────────────────────────────────────────────────

# 무서사 제외를 SQL로 민다 — limit(상한 노브)이 '이야기 항목' 기준으로 걸리게.
# 파라미터는 위 블랙리스트 상수에서 파생된다 (단일 출처).
_CHAIN_SQL = """
SELECT event_id, type, actor_id, tick, occurred_at, payload
FROM es.events
WHERE world_id = %s AND correlation_id = %s
  AND type NOT LIKE ALL(%s::text[]) AND type != ALL(%s::text[])
ORDER BY global_seq
LIMIT %s
"""

_NAMES_SQL = """
SELECT actor_id, name FROM read.actors
WHERE world_id = %s AND actor_id = ANY(%s)
"""


class StoryReads:
    """읽기 전용 사슬 질의 — pool은 psycopg_pool.AsyncConnectionPool(또는 테스트 대역)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def timeline(
        self, world_id: str, correlation_id: str, *, player_id: str | None, limit: int
    ) -> dict[str, Any]:
        """한 correlation의 사건 연쇄를 global_seq(적재) 순의 타임라인으로."""
        excluded_likes = [f"{p}%" for p in _NON_NARRATIVE_PREFIXES]
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                _CHAIN_SQL,
                (world_id, correlation_id, excluded_likes,
                 sorted(_NON_NARRATIVE_TYPES), limit),
            )).fetchall()

        names = await self._actor_names(
            world_id, {actor_id for _, _, actor_id, _, _, _ in rows if actor_id}
        )
        items = [
            {
                "event_id": event_id,
                "type": type_,
                "tick": tick,
                "occurred_at": occurred_at,
                "actor": display_actor(actor_id, payload, names=names, requester=player_id),
                "summary": summarize(type_, payload),
            }
            for event_id, type_, actor_id, tick, occurred_at, payload in rows
        ]

        # 저자성 판정 — origin이 플레이어 개입이고 그 주인이 요청자다
        # (plan/03: "이 드라마의 원작자가 나"라는 감각이 이 기능의 존재 이유)
        origin = items[0] if items else None
        started_by_you = (
            origin is not None
            and origin["type"].startswith("player.")
            and player_id is not None
            and rows[0][5].get("player_id") == player_id
        )
        return {
            "world_id": world_id,
            "correlation_id": correlation_id,
            "items": items,
            "origin": origin,
            "started_by_you": started_by_you,
        }

    async def _actor_names(self, world_id: str, actor_ids: set[str]) -> dict[str, str]:
        """read.actors 이름 해석 — 장식이다. read 미구축이 사슬 조회를 죽이면 안 된다."""
        if not actor_ids:
            return {}
        try:
            async with self._pool.connection() as conn:
                rows = await (await conn.execute(
                    _NAMES_SQL, (world_id, sorted(actor_ids))
                )).fetchall()
            return dict(rows)
        except Exception as e:
            logger.warning("read.actors 이름 해석 실패(익명 폴백): %s", e)
            return {}
