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

_CONVERSATION_SQL = """
SELECT event_id, sender, channel, text, post_id, tick, occurred_at
FROM read.messages
WHERE world_id = %s AND player_id = %s AND actor_id = %s
  AND (%s::text IS NULL OR event_id < %s)
ORDER BY event_id DESC
LIMIT %s
"""


def _rows_to_dicts(columns: tuple[str, ...], rows: list[tuple]) -> list[dict[str, Any]]:
    return [dict(zip(columns, row)) for row in rows]


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
        """정체성 + 신념 전체(확신순) + 최근 에피소드 페이지 — 액터의 겉과 속 (ADR-012/008)."""
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
        identity = (
            dict(zip(_IDENTITY_COLS, identity_row)) if identity_row is not None else None
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
