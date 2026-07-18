"""pg read 모델 무결성 검사 — 원천(es) 대비 (ADR-003 계약 3의 눈).

kuzu_verify와 같은 원칙: 검사만 하고 고치지 않는다 — 리포트가 --rebuild 판단
근거다. 슬롯 테이블(actors/arcs)은 키 집합을, append-only 테이블(episodes/
messages/arc_history)은 개수를, 신념은 (actor, kind, about) 슬롯 수를 비교한다.

은퇴(actor.identity.retired)도 원천의 일부다: 기대 계산은 "그 행의 이벤트보다
나중(event_id ULID 비교, ADR-002)에 그 액터의 은퇴가 있으면 소멸했다"로
프로젝션의 소멸 가드(_RETIRE_SQLS)와 동형이다 — 은퇴 액터 때문에 어긋나지 않는다.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg import AsyncConnection

logger = logging.getLogger("lf.projector.pg_verify")

#: (이름, 기대 SQL, 실측 SQL) — 스칼라 비교(개수/슬롯 수)
_COUNTS: tuple[tuple[str, str, str], ...] = (
    (
        "actor_episodes",
        "SELECT count(*) FROM es.events m"
        " WHERE m.world_id = %s AND m.type = 'actor.memory.consolidated'"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM es.events r"
        "   WHERE r.world_id = m.world_id AND r.type = 'actor.identity.retired'"
        "     AND r.actor_id = m.actor_id AND r.event_id > m.event_id)",
        "SELECT count(*) FROM read.actor_episodes WHERE world_id = %s",
    ),
    (
        "actor_arc_history",
        "SELECT count(*) FROM es.events a"
        " WHERE a.world_id = %s AND a.type = 'system.director.arc_planned'"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM es.events r"
        "   WHERE r.world_id = a.world_id AND r.type = 'actor.identity.retired'"
        "     AND r.actor_id = a.payload->>'target_actor_id'"
        "     AND r.event_id > a.event_id)",
        "SELECT count(*) FROM read.actor_arc_history WHERE world_id = %s",
    ),
    (
        # 소멸 규칙과 동형: ① 쓴 액터가 이후 은퇴한 actor.message.sent
        # ② 이후 은퇴한 액터의 포스트(post_id 역추적)에 달린 스레드 — 둘 다 기대 밖
        "messages",
        "SELECT count(*) FROM es.events m"
        " WHERE m.world_id = %s AND m.type IN"
        " ('player.dm.sent', 'player.comment.posted', 'actor.message.sent')"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM es.events r"
        "   WHERE r.world_id = m.world_id AND r.type = 'actor.identity.retired'"
        "     AND r.event_id > m.event_id"
        "     AND ((m.type = 'actor.message.sent' AND r.actor_id = m.actor_id)"
        "       OR EXISTS ("
        "         SELECT 1 FROM es.events p"
        "         WHERE p.world_id = m.world_id AND p.type = 'feed.post.published'"
        "           AND p.event_id = m.payload->>'post_id'"
        "           AND p.actor_id = r.actor_id)))",
        "SELECT count(*) FROM read.messages WHERE world_id = %s",
    ),
    (
        # 슬롯의 마지막 신념 이후 은퇴(품은 자·대상 어느 쪽이든)가 있으면 소멸
        "actor_beliefs",
        "SELECT count(*) FROM ("
        "   SELECT world_id, actor_id, payload->>'kind' AS kind,"
        "          COALESCE(payload->>'about_id', '-') AS about_id,"
        "          max(event_id) AS last_event"
        "   FROM es.events WHERE world_id = %s AND type = 'actor.belief.formed'"
        "   GROUP BY 1, 2, 3, 4) s"
        " WHERE NOT EXISTS ("
        "   SELECT 1 FROM es.events r"
        "   WHERE r.world_id = s.world_id AND r.type = 'actor.identity.retired'"
        "     AND r.actor_id IN (s.actor_id, s.about_id)"
        "     AND r.event_id > s.last_event)",
        "SELECT count(*) FROM read.actor_beliefs WHERE world_id = %s",
    ),
)

#: (이름, 기대 키 SQL, 실측 키 SQL) — 슬롯 테이블은 키 집합까지 비교
_KEY_SETS: tuple[tuple[str, str, str], ...] = (
    (
        "actors",
        "SELECT DISTINCT d.actor_id FROM es.events d"
        " WHERE d.world_id = %s AND d.type = 'actor.identity.declared'"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM es.events r"
        "   WHERE r.world_id = d.world_id AND r.type = 'actor.identity.retired'"
        "     AND r.actor_id = d.actor_id AND r.event_id > d.event_id)",
        "SELECT actor_id FROM read.actors WHERE world_id = %s",
    ),
    (
        "actor_arcs",
        "SELECT DISTINCT a.payload->>'target_actor_id' FROM es.events a"
        " WHERE a.world_id = %s AND a.type = 'system.director.arc_planned'"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM es.events r"
        "   WHERE r.world_id = a.world_id AND r.type = 'actor.identity.retired'"
        "     AND r.actor_id = a.payload->>'target_actor_id'"
        "     AND r.event_id > a.event_id)",
        "SELECT actor_id FROM read.actor_arcs WHERE world_id = %s",
    ),
)

#: 원천 ∪ 프로젝션 — read 쪽 세계를 합쳐야 고아 프로젝션(원천에 없는 세계의
#: 잔여 행)이 보인다. 원천만 보면 그런 세계는 검사 자체가 건너뛰어진다.
_WORLDS_SQL = (
    "SELECT DISTINCT world_id FROM es.events"
    " UNION SELECT world_id FROM read.actors"
    " UNION SELECT world_id FROM read.actor_arcs"
    " UNION SELECT world_id FROM read.actor_arc_history"
    " UNION SELECT world_id FROM read.actor_episodes"
    " UNION SELECT world_id FROM read.messages"
    " UNION SELECT world_id FROM read.actor_beliefs"
)


async def verify_pg_world(conn: AsyncConnection, world_id: str) -> dict[str, Any]:
    """세계 하나의 read 스키마 무결성 리포트 — 표별 ok와 어긋남 요약."""
    tables: dict[str, dict[str, Any]] = {}
    for name, expected_sql, actual_sql in _COUNTS:
        expected = (await (await conn.execute(expected_sql, (world_id,))).fetchone())[0]
        actual = (await (await conn.execute(actual_sql, (world_id,))).fetchone())[0]
        tables[name] = {"ok": expected == actual, "expected": expected, "actual": actual}
    for name, expected_sql, actual_sql in _KEY_SETS:
        expected = {r[0] for r in await (await conn.execute(expected_sql, (world_id,))).fetchall()}
        actual = {r[0] for r in await (await conn.execute(actual_sql, (world_id,))).fetchall()}
        tables[name] = {
            "ok": expected == actual,
            "expected": len(expected),
            "actual": len(actual),
            "missing": sorted(expected - actual),
            "extra": sorted(actual - expected),
        }
    report = {"ok": all(t["ok"] for t in tables.values()), "tables": tables}
    logger.info(
        "pg 무결성 %s — world=%s %s", "OK" if report["ok"] else "MISMATCH", world_id,
        {k: v["ok"] for k, v in tables.items()},
    )
    return report


async def verify_pg(
    conn: AsyncConnection, *, world_id: str | None = None
) -> dict[str, Any]:
    """세계별 pg 무결성 — world_id를 주면 그 세계만, 아니면 원천의 전 세계."""
    if world_id is not None:
        worlds = [world_id]
    else:
        rows = await (await conn.execute(_WORLDS_SQL)).fetchall()
        worlds = sorted(w for (w,) in rows)
    reports = {world: await verify_pg_world(conn, world) for world in worlds}
    return {"ok": all(r["ok"] for r in reports.values()), "worlds": reports}
