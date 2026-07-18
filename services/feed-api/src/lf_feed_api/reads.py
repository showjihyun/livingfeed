"""PG read 테이블 조회 — 액터 프로필·대화 히스토리 (ADR-003 읽기 API 규칙).

read 스키마(pg-projector의 산출물)만 읽는다 — 이벤트 스토어(es)는 절대
질의하지 않는다. 커서는 event_id(ULID) 내림차순, /feed recent와 같은 좌표계.
"""

from __future__ import annotations

from typing import Any

_IDENTITY_SQL = """
SELECT actor_id, name, archetype, bio, goals
FROM read.actors
WHERE world_id = %s AND actor_id = %s
"""

_ACTORS_SQL = """
SELECT actor_id, name, archetype, bio, goals
FROM read.actors
WHERE world_id = %s
ORDER BY actor_id
"""

_BELIEFS_SQL = """
SELECT kind, NULLIF(about_id, '-') AS about_id, statement, confidence,
       source_event_ids, event_id, first_formed_at, updated_at, revisions
FROM read.actor_beliefs
WHERE world_id = %s AND actor_id = %s
ORDER BY confidence DESC, kind, about_id
"""

_IDENTITY_COLS = ("actor_id", "name", "archetype", "bio", "goals")

_EPISODES_SQL = """
SELECT event_id, tick, occurred_at, summary, importance, factors, tags, source_event_ids
FROM read.actor_episodes
WHERE world_id = %s AND actor_id = %s AND (%s::text IS NULL OR event_id < %s)
ORDER BY event_id DESC
LIMIT %s
"""

_ARC_SQL = """
SELECT stage, intention, planned_at
FROM read.actor_arcs
WHERE world_id = %s AND actor_id = %s
"""

_ARC_HISTORY_SQL = """
SELECT stage, intention, planned_at
FROM read.actor_arc_history
WHERE world_id = %s AND actor_id = %s
ORDER BY event_id
"""

_CONVERSATION_SQL = """
SELECT event_id, sender, channel, text, post_id, tick, occurred_at
FROM read.messages
WHERE world_id = %s AND player_id = %s AND actor_id = %s
  AND (%s::text IS NULL OR event_id < %s)
ORDER BY event_id DESC
LIMIT %s
"""

# 포스트별 댓글 스레드 — 시간 오름차순(대화는 처음부터 읽는다). 작성자는
# sender에 따라 갈린다: player 댓글은 player_id, actor 댓글(답글·소셜 루프)은
# actor_id가 저자다. 액터 이름은 read.actors에서 동봉한다 (이름 그라운딩 —
# FE가 원시 id를 화면에 싣지 않게 한다). 플레이어 이름 read 모델은 아직 없다 — null.
_POST_COMMENTS_SQL = """
SELECT m.event_id,
       m.sender,
       CASE WHEN m.sender = 'player' THEN m.player_id ELSE m.actor_id END AS author_id,
       a.name AS author_name,
       m.text, m.in_reply_to, m.tick, m.occurred_at
FROM read.messages m
LEFT JOIN read.actors a
    ON a.world_id = m.world_id
   AND m.sender = 'actor' AND a.actor_id = m.actor_id
WHERE m.world_id = %s AND m.channel = 'comment' AND m.post_id = %s
ORDER BY m.event_id
LIMIT %s
"""

# 액터별 마지막 메시지 1건 → 최신 대화 순 — 인박스 목록의 좌표계도 event_id(ULID)다.
# player_id 필터가 액터↔액터 대화(counterpart가 a_*)를 자연히 걸러낸다 (pg_read 참고).
_THREADS_SQL = """
SELECT actor_id, sender, channel, text, event_id, occurred_at
FROM (
    SELECT DISTINCT ON (actor_id)
           actor_id, sender, channel, text, event_id, occurred_at
    FROM read.messages
    WHERE world_id = %s AND player_id = %s
    ORDER BY actor_id, event_id DESC
) AS last_by_actor
ORDER BY event_id DESC
LIMIT %s
"""


def _rows_to_dicts(columns: tuple[str, ...], rows: list[tuple]) -> list[dict[str, Any]]:
    return [dict(zip(columns, row, strict=True)) for row in rows]


class ProfileReads:
    """읽기 전용 질의 — pool은 psycopg_pool.AsyncConnectionPool(또는 테스트 대역)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def actors(self, world_id: str) -> list[dict[str, Any]]:
        """세계의 액터 명단 — FE가 이름·소개를 하드코딩하지 않고 여기서 읽는다 (ADR-012)."""
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(_ACTORS_SQL, (world_id,))).fetchall()
        return _rows_to_dicts(_IDENTITY_COLS, rows)

    async def actor_profile(
        self, world_id: str, actor_id: str, *, episode_limit: int, episode_cursor: str | None
    ) -> dict[str, Any]:
        """정체성 + 신념(확신순) + 에피소드 페이지 + 인생 아크 — 액터의 겉과 속과 방향
        (ADR-012/008/013)."""
        async with self._pool.connection() as conn:
            identity_row = await (await conn.execute(
                _IDENTITY_SQL, (world_id, actor_id)
            )).fetchone()
            beliefs = await (await conn.execute(
                _BELIEFS_SQL, (world_id, actor_id)
            )).fetchall()
            episodes = await (await conn.execute(
                _EPISODES_SQL,
                (world_id, actor_id, episode_cursor, episode_cursor, episode_limit),
            )).fetchall()
            arc_row = await (await conn.execute(
                _ARC_SQL, (world_id, actor_id)
            )).fetchone()
            arc_history = await (await conn.execute(
                _ARC_HISTORY_SQL, (world_id, actor_id)
            )).fetchall()
        identity = (
            dict(zip(_IDENTITY_COLS, identity_row, strict=True))
            if identity_row is not None else None
        )
        episode_items = _rows_to_dicts(
            ("event_id", "tick", "occurred_at", "summary", "importance",
             "factors", "tags", "source_event_ids"),
            episodes,
        )
        return {
            "world_id": world_id,
            "actor_id": actor_id,
            "identity": identity,
            "beliefs": _rows_to_dicts(
                ("kind", "about_id", "statement", "confidence", "source_event_ids",
                 "event_id", "first_formed_at", "updated_at", "revisions"),
                beliefs,
            ),
            "episodes": {
                "items": episode_items,
                "next_cursor": episode_items[-1]["event_id"] if episode_items else None,
            },
            # 인생 아크 — Director가 그린 이번 시즌의 방향 (없으면 null — 그저 일상)
            "arc": (
                dict(zip(("stage", "intention", "planned_at"), arc_row, strict=True))
                if arc_row is not None else None
            ),
            # 인생의 연대기 — 장의 흐름, 오래된 순 (append-only 이력)
            "arc_history": _rows_to_dicts(
                ("stage", "intention", "planned_at"), arc_history
            ),
        }

    async def threads(
        self, world_id: str, player_id: str, *, limit: int
    ) -> dict[str, Any]:
        """플레이어의 대화 상대 목록 — 액터별 마지막 메시지 1건, 최신 대화 순.

        선제 DM(액터가 먼저 건 말)도 그저 대화의 첫 마디다 — 특별 취급 없이
        같은 스레드 집계에 실린다 (다중 대화 인박스의 목록 뷰 데이터).
        """
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                _THREADS_SQL, (world_id, player_id, limit)
            )).fetchall()
        return {
            "threads": [
                {
                    "actor_id": actor_id,
                    "last_text": text,
                    "last_channel": channel,
                    "last_at": occurred_at,
                    "last_event_id": event_id,
                    "last_from_actor": sender == "actor",
                }
                for actor_id, sender, channel, text, event_id, occurred_at in rows
            ],
        }

    async def post_comments(
        self, world_id: str, post_id: str, *, limit: int
    ) -> dict[str, Any]:
        """포스트에 달린 댓글 스레드 — 플레이어·액터 모두, 시간순 (도파민 루프의
        되읽기: 새로고침해도 개입의 흔적이 남는다).

        author_kind로 player/actor를 가르고, 액터 저자는 표시 이름을 동봉한다.
        in_reply_to가 post_id와 다르면 다른 댓글에 대한 답장이다 (깊이 1 스레드).
        """
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                _POST_COMMENTS_SQL, (world_id, post_id, limit)
            )).fetchall()
        return {
            "post_id": post_id,
            "items": [
                {
                    "event_id": event_id,
                    "author_kind": sender,
                    "author_id": author_id,
                    # player 저자는 null — 표시명은 FE의 자기표시('나') 몫
                    "author_name": author_name,
                    "text": text,
                    "in_reply_to": in_reply_to,
                    "tick": tick,
                    "occurred_at": occurred_at,
                }
                for event_id, sender, author_id, author_name,
                    text, in_reply_to, tick, occurred_at in rows
            ],
        }

    async def conversation(
        self, world_id: str, player_id: str, actor_id: str, *, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        """플레이어↔액터 대화 히스토리 (양방향, 시간 역순) — WS 재접속 이어보기용."""
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                _CONVERSATION_SQL, (world_id, player_id, actor_id, cursor, cursor, limit)
            )).fetchall()
        items = _rows_to_dicts(
            ("event_id", "sender", "channel", "text", "post_id", "tick", "occurred_at"),
            rows,
        )
        return {
            "items": items,
            "next_cursor": items[-1]["event_id"] if items else None,
            "mode": "recent",
        }
