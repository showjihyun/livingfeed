"""PG read 테이블 — 웹 클라이언트용 프로필/목록 프로젝션 (ADR-003, ADR-008 후속).

이벤트 스토어(es 스키마)와 같은 PG 인스턴스의 read 스키마를 쓴다.
프로젝션은 소모품이다 (ADR-003 계약 3): 마이그레이션 도구 없이 멱등 DDL만 두고,
스키마 변경 = drop() 후 --rebuild.

테이블:
- actor_episodes: actor.memory.consolidated — 액터의 응고 기억 목록 (ADR-008)
- actor_beliefs:  actor.belief.formed — (kind, about) 자리 단위 최신 신념.
  같은 자리 재발행은 갱신이다 (reflection 계약과 동일)
- messages:       player.dm.sent / player.comment.posted / actor.message.sent —
  플레이어↔액터 대화 히스토리 (WS 재접속 시 이어보기) + 포스트별 댓글
  스레드 (post_id 조회 — 새로고침에도 댓글이 남는다)
- actor_arcs:     system.director.arc_planned — 액터별 현재 인생 아크
  (ADR-013/plan-08, 프로필 "인생의 장"). 다음 계획이 자리를 덮어쓴다
- actor_arc_history: 같은 이벤트의 append-only 연대기 — 장의 흐름이 남는다
  (프로필 "인생의 연대기"). 한 이벤트가 두 테이블에 프로젝션되는 첫 사례
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
    -- 답장 대상 event_id (actor.message.sent 페이로드 보존) — 댓글 스레드에서
    -- 최상위(=post_id/null)와 답장을 가른다. 플레이어 발신은 항상 최상위(null)
    in_reply_to TEXT,
    tick        BIGINT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation
    ON read.messages (world_id, player_id, actor_id, event_id DESC);
-- 포스트별 댓글 스레드 조회 (feed-api /posts/{id}/comments) — event_id 오름차순 = 시간순
CREATE INDEX IF NOT EXISTS messages_post_thread
    ON read.messages (world_id, post_id, event_id)
    WHERE post_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS read.actors (
    world_id     TEXT NOT NULL,
    actor_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    archetype    TEXT NOT NULL,
    bio          TEXT NOT NULL,
    goals        JSONB NOT NULL,
    event_id     TEXT NOT NULL,
    declared_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (world_id, actor_id)
);

CREATE TABLE IF NOT EXISTS read.actor_arcs (
    world_id    TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    stage       TEXT NOT NULL,
    intention   TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    planned_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (world_id, actor_id)
);

CREATE TABLE IF NOT EXISTS read.actor_arc_history (
    event_id    TEXT PRIMARY KEY,
    world_id    TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    stage       TEXT NOT NULL,
    intention   TEXT NOT NULL,
    planned_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS actor_arc_history_chrono
    ON read.actor_arc_history (world_id, actor_id, event_id);
"""

TABLES = (
    "read.actor_episodes", "read.actor_beliefs", "read.messages",
    "read.actors", "read.actor_arcs", "read.actor_arc_history",
)

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
     text, post_id, in_reply_to, tick, occurred_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)
ON CONFLICT (event_id) DO NOTHING
"""

#: 아크는 (world, actor) 자리 단위 upsert — 다음 계획이 덮어쓴다 (ArcStore와 동일 규약)
_ARC_SQL = """
INSERT INTO read.actor_arcs
    (world_id, actor_id, stage, intention, event_id, planned_at)
VALUES (%s, %s, %s, %s, %s, %s::timestamptz)
ON CONFLICT (world_id, actor_id) DO UPDATE SET
    stage      = excluded.stage,
    intention  = excluded.intention,
    event_id   = excluded.event_id,
    planned_at = excluded.planned_at
WHERE excluded.event_id > read.actor_arcs.event_id
"""

#: 연대기는 append-only — 같은 이벤트 재전달만 거른다 (장의 흐름이 이력으로 남는다)
_ARC_HISTORY_SQL = """
INSERT INTO read.actor_arc_history
    (event_id, world_id, actor_id, stage, intention, planned_at)
VALUES (%s, %s, %s, %s, %s, %s::timestamptz)
ON CONFLICT (event_id) DO NOTHING
"""

#: 정체성은 (world, actor) 자리 단위 upsert — 재선언(재시작 등)은 최신 ULID가 이긴다
_ACTOR_SQL = """
INSERT INTO read.actors
    (world_id, actor_id, name, archetype, bio, goals, event_id, declared_at)
VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s::timestamptz)
ON CONFLICT (world_id, actor_id) DO UPDATE SET
    name        = excluded.name,
    archetype   = excluded.archetype,
    bio         = excluded.bio,
    goals       = excluded.goals,
    event_id    = excluded.event_id,
    declared_at = excluded.declared_at
WHERE excluded.event_id > read.actors.event_id
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
    """세 대화 이벤트를 한 테이블로 정규화한다 — 대화는 방향이 다른 같은 데이터다.

    액터→액터 댓글(소셜 루프)은 target_player_id가 null이다 — 상대(counterpart)
    열에는 target_actor_id가 들어간다. /messages 조회는 p_* 기준이라 플레이어
    대화 화면을 오염시키지 않는다.
    """
    p = envelope["payload"]
    kind = envelope["type"]
    if kind == "actor.message.sent":
        counterpart = p["target_player_id"] or p.get("target_actor_id")
        row = (p["channel"], counterpart, envelope["actor_id"], "actor",
               p["text"], p["post_id"], p["in_reply_to"])
    elif kind == "player.dm.sent":
        row = ("dm", p["player_id"], p["target_actor_id"], "player", p["text"], None, None)
    elif kind == "player.comment.posted":
        # 플레이어 댓글은 항상 포스트 직속(최상위)이다 — in_reply_to 페이로드가 없다
        row = ("comment", p["player_id"], p["target_actor_id"], "player",
               p["text"], p["post_id"], None)
    else:
        raise KeyError(f"대화 이벤트가 아니다: {kind}")
    return (
        envelope["event_id"], envelope["world_id"], *row,
        envelope["tick"], envelope["occurred_at"],
    )


def actor_params(envelope: dict[str, Any]) -> tuple:
    p = envelope["payload"]
    return (
        envelope["world_id"], envelope["actor_id"], p["name"], p["archetype"],
        p["bio"], json.dumps(p["goals"], ensure_ascii=False),
        envelope["event_id"], envelope["occurred_at"],
    )


def arc_params(envelope: dict[str, Any]) -> tuple:
    """아크는 Director 제어 신호 — 봉투 actor_id가 null이라 대상은 payload에 있다."""
    p = envelope["payload"]
    return (
        envelope["world_id"], p["target_actor_id"], p["stage"], p["intention"],
        envelope["event_id"], envelope["occurred_at"],
    )


def arc_history_params(envelope: dict[str, Any]) -> tuple:
    p = envelope["payload"]
    return (
        envelope["event_id"], envelope["world_id"], p["target_actor_id"],
        p["stage"], p["intention"], envelope["occurred_at"],
    )


#: type → ((SQL, 파라미터 변환), ...) — 한 이벤트가 여러 테이블로 갈 수 있다
#: (아크: 현재 자리 + 연대기). 목록에 없는 타입은 프로젝션 대상이 아니다 (전방 호환 무시)
PROJECTIONS: dict[str, tuple[tuple[str, Any], ...]] = {
    "actor.memory.consolidated": ((_EPISODE_SQL, episode_params),),
    "actor.belief.formed": ((_BELIEF_SQL, belief_params),),
    "actor.identity.declared": ((_ACTOR_SQL, actor_params),),
    "actor.message.sent": ((_MESSAGE_SQL, message_params),),
    "player.dm.sent": ((_MESSAGE_SQL, message_params),),
    "player.comment.posted": ((_MESSAGE_SQL, message_params),),
    "system.director.arc_planned": (
        (_ARC_SQL, arc_params),
        (_ARC_HISTORY_SQL, arc_history_params),
    ),
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
        """봉투 하나를 반영한다 (타입에 걸린 전 테이블). 반환: 프로젝션 대상이었는가."""
        projections = PROJECTIONS.get(envelope["type"])
        if projections is None:
            return False
        for sql, params in projections:
            await self._conn.execute(sql, params(envelope))
        return True
