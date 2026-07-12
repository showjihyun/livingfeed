"""PG read 테이블 — 웹 클라이언트용 프로필/목록 프로젝션 (ADR-003, ADR-008 후속).

이벤트 스토어(es 스키마)와 같은 PG 인스턴스의 read 스키마를 쓴다.
프로젝션은 소모품이다 (ADR-003 계약 3): 마이그레이션 도구 없이 멱등 DDL만 두고,
스키마 변경 = drop() 후 --rebuild.

테이블:
- actor_episodes: actor.memory.consolidated — 액터의 응고 기억 목록 (ADR-008)
- actor_beliefs:  actor.belief.formed — (kind, about) 자리 단위 최신 신념.
  같은 자리 재발행은 갱신이다 (reflection 계약과 동일)
- messages:       player.dm.sent / player.comment.posted / actor.message.sent —
  플레이어↔액터 대화 히스토리 (WS 재접속 시 이어보기)
"""

from __future__ import annotations

import json
from typing import Any

from psycopg import AsyncConnection

DDL = """
CREATE SCHEMA IF NOT EXISTS read;

CREATE TABLE IF NOT EXISTS read.actor_episodes (
    event_id         TEXT PRIMARY KEY,
    world_id         TEXT NOT NULL,
    actor_id         TEXT NOT NULL,
    tick             BIGINT NOT NULL,
    occurred_at      TIMESTAMPTZ NOT NULL,
    summary          TEXT NOT NULL,
    importance       REAL NOT NULL,
    factors          JSONB NOT NULL,
    tags             TEXT[] NOT NULL,
    source_event_ids TEXT[] NOT NULL
);
-- event_id(ULID) 내림차순 = 시간 역순 — /feed recent와 같은 커서 좌표계 (ADR-010)
CREATE INDEX IF NOT EXISTS actor_episodes_recent
    ON read.actor_episodes (world_id, actor_id, event_id DESC);

CREATE TABLE IF NOT EXISTS read.actor_beliefs (
    world_id         TEXT NOT NULL,
    actor_id         TEXT NOT NULL,
    kind             TEXT NOT NULL,
    about_id         TEXT NOT NULL,
    statement        TEXT NOT NULL,
    confidence       REAL NOT NULL,
    source_event_ids TEXT[] NOT NULL,
    event_id         TEXT NOT NULL,
    first_formed_at  TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL,
    revisions        INTEGER NOT NULL,
    PRIMARY KEY (world_id, actor_id, kind, about_id)
);

CREATE TABLE IF NOT EXISTS read.messages (
    event_id    TEXT PRIMARY KEY,
    world_id    TEXT NOT NULL,
    channel     TEXT NOT NULL,
    player_id   TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    sender      TEXT NOT NULL,
    text        TEXT NOT NULL,
    post_id     TEXT,
    tick        BIGINT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation
    ON read.messages (world_id, player_id, actor_id, event_id DESC);
"""

TABLES = ("read.actor_episodes", "read.actor_beliefs", "read.messages")

#: about_id null(자기 자신/세계에 대한 신념)의 PK 표현 — reflection의 자리 키와 동일 규약
NO_ABOUT = "-"

_EPISODE_SQL = """
INSERT INTO read.actor_episodes
    (event_id, world_id, actor_id, tick, occurred_at, summary,
     importance, factors, tags, source_event_ids)
VALUES (%s, %s, %s, %s, %s::timestamptz, %s, %s, %s::jsonb, %s, %s)
ON CONFLICT (event_id) DO NOTHING
"""

#: 재전달(같은 event_id)과 순서 뒤집힘(과거 event_id)을 한 가드로 거른다 —
#: ULID 문자열 비교가 곧 시간 비교다 (ADR-002)
_BELIEF_SQL = """
INSERT INTO read.actor_beliefs
    (world_id, actor_id, kind, about_id, statement, confidence,
     source_event_ids, event_id, first_formed_at, updated_at, revisions)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz, 1)
ON CONFLICT (world_id, actor_id, kind, about_id) DO UPDATE SET
    statement        = excluded.statement,
    confidence       = excluded.confidence,
    source_event_ids = excluded.source_event_ids,
    event_id         = excluded.event_id,
    updated_at       = excluded.updated_at,
    revisions        = read.actor_beliefs.revisions + 1
WHERE excluded.event_id > read.actor_beliefs.event_id
"""

_MESSAGE_SQL = """
INSERT INTO read.messages
    (event_id, world_id, channel, player_id, actor_id, sender,
     text, post_id, tick, occurred_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
ON CONFLICT (event_id) DO NOTHING
"""


def episode_params(envelope: dict[str, Any]) -> tuple:
    p = envelope["payload"]
    return (
        envelope["event_id"], envelope["world_id"], envelope["actor_id"],
        envelope["tick"], envelope["occurred_at"], p["summary"],
        p["importance"], json.dumps(p["factors"]), p["tags"], p["source_event_ids"],
    )


def belief_params(envelope: dict[str, Any]) -> tuple:
    p = envelope["payload"]
    return (
        envelope["world_id"], envelope["actor_id"], p["kind"],
        p["about_id"] or NO_ABOUT, p["statement"], p["confidence"],
        p["source_event_ids"], envelope["event_id"],
        envelope["occurred_at"], envelope["occurred_at"],
    )


def message_params(envelope: dict[str, Any]) -> tuple:
    """세 대화 이벤트를 한 테이블로 정규화한다 — 대화는 방향이 다른 같은 데이터다."""
    p = envelope["payload"]
    kind = envelope["type"]
    if kind == "actor.message.sent":
        row = (p["channel"], p["target_player_id"], envelope["actor_id"], "actor",
               p["text"], p["post_id"])
    elif kind == "player.dm.sent":
        row = ("dm", p["player_id"], p["target_actor_id"], "player", p["text"], None)
    elif kind == "player.comment.posted":
        row = ("comment", p["player_id"], p["target_actor_id"], "player",
               p["text"], p["post_id"])
    else:
        raise KeyError(f"대화 이벤트가 아니다: {kind}")
    return (
        envelope["event_id"], envelope["world_id"], *row,
        envelope["tick"], envelope["occurred_at"],
    )


#: type → (SQL, 파라미터 변환) — 목록에 없는 타입은 프로젝션 대상이 아니다 (전방 호환 무시)
PROJECTIONS: dict[str, tuple[str, Any]] = {
    "actor.memory.consolidated": (_EPISODE_SQL, episode_params),
    "actor.belief.formed": (_BELIEF_SQL, belief_params),
    "actor.message.sent": (_MESSAGE_SQL, message_params),
    "player.dm.sent": (_MESSAGE_SQL, message_params),
    "player.comment.posted": (_MESSAGE_SQL, message_params),
}


class ReadStore:
    """read 스키마에 대한 최소 클라이언트 — ensure/drop/apply (ADR-003 계약 1·3)."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def ensure(self) -> None:
        await self._conn.execute(DDL)

    async def drop(self) -> None:
        for table in TABLES:
            await self._conn.execute(f"DROP TABLE IF EXISTS {table}")

    async def apply(self, envelope: dict[str, Any]) -> bool:
        """봉투 하나를 반영한다. 반환: 프로젝션 대상 타입이었는가."""
        projection = PROJECTIONS.get(envelope["type"])
        if projection is None:
            return False
        sql, params = projection
        await self._conn.execute(sql, params(envelope))
        return True
