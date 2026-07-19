"""es(SoT) 기반 완전 재구축 — JetStream 보존 한도 밖까지 리플레이 (ADR-003 계약 3 완결).

--rebuild --once의 재소비 원천은 NATS 스트림이라 보존 한도를 넘긴 과거는
되세울 수 없다. from-es는 NATS를 전혀 거치지 않는다: es.events에서 봉투를
재구성해 kind별 apply에 직접 먹인다 — global_seq 오름차순 = 적재(발행) 순서라
스트림 내 순서가 보존되고, 봉투는 outbox 원본(read_stream과 같은 재구성)과
동일하다. durable은 건드리지 않는다: 서비스 재기동이 체크포인트부터 이어가고,
리플레이와의 겹침은 프로젝션 멱등이 흡수한다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection

#: kind → 소비 타입 술어 — 각 프로젝터의 NATS filter subject와 동형 (".>"는 접두).
#: 리터럴로 둔다: 프로젝터 모듈을 import하면 순환이 생기고, 동형성은 테스트가 고정한다.
PATTERNS: dict[str, tuple[str, ...]] = {
    "os": ("feed.post.published", "actor.identity.retired", "actor.identity.returned"),
    "kuzu": ("relationship.>", "actor.identity.retired", "actor.identity.returned"),
    "pg": ("actor.>", "player.>", "system.>"),
    "redis": (
        "relationship.>", "player.follow.changed",
        "feed.post.published", "actor.message.sent",
        "actor.identity.retired", "actor.identity.returned",
    ),
}


def matches(pattern: str, type_: str) -> bool:
    """NATS filter subject와 동형의 타입 술어 — ".>"는 접두, 그 외 완전 일치."""
    if pattern.endswith(".>"):
        return type_.startswith(pattern[:-1])
    return type_ == pattern


def split_patterns(patterns: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """술어를 SQL로 밀기 위한 분해 — 접두는 LIKE 패턴, 나머지는 완전 일치.

    타입엔 '_'가 올 수 있어(arc_planned 등) 완전 일치를 LIKE로 뭉개지 않는다.
    """
    likes = [f"{p[:-1]}%" for p in patterns if p.endswith(".>")]
    exacts = [p for p in patterns if not p.endswith(".>")]
    return likes, exacts


_REPLAY_SQL = """
SELECT global_seq, event_id, stream, type, schema_version, world_id, actor_id,
       tick, occurred_at, causation_id, correlation_id, payload
FROM es.events
WHERE global_seq > %s AND (type LIKE ANY(%s::text[]) OR type = ANY(%s::text[]))
ORDER BY global_seq
LIMIT %s
"""


def _isoformat_z(dt: datetime) -> str:
    # 세션 TZ와 무관하게 outbox 원본과 같은 표기(Z)로 — 봉투 동일성의 조건
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _envelope(row: tuple) -> dict[str, Any]:
    """es 행 → 봉투 — lf_eventstore.read_stream과 같은 재구성 (outbox 원본과 동일)."""
    return {
        "event_id": row[1],
        "stream": row[2],
        "type": row[3],
        "schema_version": row[4],
        "world_id": row[5],
        "actor_id": row[6],
        "tick": row[7],
        "occurred_at": _isoformat_z(row[8]),
        "causation_id": row[9],
        "correlation_id": row[10],
        "payload": row[11],
    }


async def replay_envelopes(
    conn: AsyncConnection, patterns: tuple[str, ...], *, batch_size: int = 500
) -> AsyncIterator[dict[str, Any]]:
    """es를 global_seq 순으로 훑어 술어에 맞는 봉투를 낸다 (키셋 페이지네이션)."""
    likes, exacts = split_patterns(patterns)
    last = 0
    while True:
        cur = await conn.execute(_REPLAY_SQL, (last, likes, exacts, batch_size))
        rows = await cur.fetchall()
        if not rows:
            return
        for row in rows:
            yield _envelope(row)
        last = rows[-1][0]


#: kind → 부활(actor.identity.returned) 재사영 범위 — 은퇴가 지운 것과 정확히 대칭인
#: es WHERE 술어. 라이프사이클 이벤트(retired/returned)는 범위 밖이다: 과거의 은퇴를
#: 다시 먹으면 되살린 것을 도로 지우고, 과거의 부활은 재귀가 된다 — 부활은 "은퇴
#: 직전 상태"의 복원이지 역사의 재상영이 아니다.
RETURN_SCOPES: dict[str, str] = {
    # 정체성·기억·신념·그의 메시지(stream=actor) + 그를 향한 신념 + 그의 아크
    # + 그의 포스트에 달린 스레드(post_id를 es에서 역추적 — _RETIRE_THREADS_SQL의 역)
    "pg": (
        "(e.stream = 'actor' AND e.actor_id = %(a)s"
        " AND e.type NOT IN ('actor.identity.retired', 'actor.identity.returned'))"
        " OR (e.type = 'actor.belief.formed' AND e.payload->>'about_id' = %(a)s)"
        " OR (e.type = 'system.director.arc_planned'"
        " AND e.payload->>'target_actor_id' = %(a)s)"
        " OR (e.type IN ('player.comment.posted', 'actor.message.sent')"
        " AND e.payload->>'post_id' IN ("
        "SELECT p.event_id FROM es.events p"
        " WHERE p.world_id = %(w)s AND p.type = 'feed.post.published'"
        " AND p.actor_id = %(a)s))"
    ),
    # 그에 닿은 양방향 관계 전부 — 스트림 키는 "from|to"라 양쪽을 재스캔한다
    # (DETACH DELETE가 지운 노드+양방향 간선과 대칭)
    "kuzu": (
        "e.stream = 'relationship'"
        " AND (split_part(e.stream_key, '|', 1) = %(a)s"
        " OR split_part(e.stream_key, '|', 2) = %(a)s)"
    ),
    # 팔로워 인덱스·거부 마커(관계 stand-in + 명시 팔로우) + 타임라인 발신분
    # (그의 포스트 팬아웃·답장·관계 리시트) — retire_actor가 걷은 것과 대칭
    "redis": (
        "(e.stream = 'relationship'"
        " AND (split_part(e.stream_key, '|', 1) = %(a)s"
        " OR split_part(e.stream_key, '|', 2) = %(a)s))"
        " OR (e.type = 'player.follow.changed'"
        " AND e.payload->>'target_actor_id' = %(a)s)"
        " OR (e.type IN ('feed.post.published', 'actor.message.sent')"
        " AND e.actor_id = %(a)s)"
    ),
    # 작성자 일치 포스트 문서만 — delete_by_actor(retire_query)와 대칭
    "os": "e.type = 'feed.post.published' AND e.actor_id = %(a)s",
}

_RETURN_SQL = """
SELECT global_seq, event_id, stream, type, schema_version, world_id, actor_id,
       tick, occurred_at, causation_id, correlation_id, payload
FROM es.events e
WHERE e.world_id = %(w)s AND e.event_id < %(boundary)s AND e.global_seq > %(last)s
  AND ({scope})
ORDER BY e.global_seq
LIMIT %(batch)s
"""


async def return_envelopes(
    conn: AsyncConnection, kind: str, envelope: dict[str, Any], *, batch_size: int = 500
) -> AsyncIterator[dict[str, Any]]:
    """부활 재사영 원천 — returned 봉투가 가리키는 액터 범위의 es를 시간순으로 낸다.

    범위는 returned의 ULID 이전(경계 미포함)이라 재은퇴(retired→returned→retired)는
    마지막 returned 시점 기준으로 되살리고, 이후의 재은퇴가 더 큰 ULID 가드로 다시
    지운다. 봉투 재구성이 replay_envelopes와 동일(_envelope)하므로 각 프로젝터의
    기존 apply 경로에 그대로 다시 먹인다 — 라이브 소비와 from-es 리플레이가 같은
    함수를 부른다 (결정적 부활, ADR-003 계약 3).
    """
    sql = _RETURN_SQL.format(scope=RETURN_SCOPES[kind])
    params: dict[str, Any] = {
        "w": envelope["world_id"],
        "a": envelope["payload"]["actor_id"],
        "boundary": envelope["event_id"],
        "last": 0,
        "batch": batch_size,
    }
    while True:
        rows = await (await conn.execute(sql, params)).fetchall()
        if not rows:
            return
        for row in rows:
            yield _envelope(row)
        params["last"] = rows[-1][0]


async def replay_into(
    conn: AsyncConnection,
    patterns: tuple[str, ...],
    apply: Callable[[dict[str, Any]], Awaitable[Any]],
    *,
    batch_size: int = 500,
) -> int:
    """리플레이 봉투를 apply에 차례로 먹인다. 반환: 먹인 봉투 수."""
    fed = 0
    async for envelope in replay_envelopes(conn, patterns, batch_size=batch_size):
        await apply(envelope)
        fed += 1
    return fed
