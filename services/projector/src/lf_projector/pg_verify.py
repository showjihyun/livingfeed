"""pg read 모델 무결성 검사 — 원천(es) 대비 (ADR-003 계약 3의 눈).

kuzu_verify와 같은 원칙: 검사만 하고 고치지 않는다 — 리포트가 --rebuild 판단
근거다. 슬롯 테이블(actors/arcs)은 키 집합을, append-only 테이블(episodes/
messages/arc_history)은 개수를, 신념은 (actor, kind, about) 슬롯 수를 비교한다.
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
        "SELECT count(*) FROM es.events"
        " WHERE world_id = %s AND type = 'actor.memory.consolidated'",
        "SELECT count(*) FROM read.actor_episodes WHERE world_id = %s",
    ),
    (
        "actor_arc_history",
        "SELECT count(*) FROM es.events"
        " WHERE world_id = %s AND type = 'system.director.arc_planned'",
        "SELECT count(*) FROM read.actor_arc_history WHERE world_id = %s",
    ),
    (
        "messages",
        "SELECT count(*) FROM es.events WHERE world_id = %s AND type IN"
        " ('player.dm.sent', 'player.comment.posted', 'actor.message.sent')",
        "SELECT count(*) FROM read.messages WHERE world_id = %s",
    ),
    (
        "actor_beliefs",
        "SELECT count(DISTINCT (actor_id, payload->>'kind',"
        " COALESCE(payload->>'about_id', '-'))) FROM es.events"
        " WHERE world_id = %s AND type = 'actor.belief.formed'",
        "SELECT count(*) FROM read.actor_beliefs WHERE world_id = %s",
    ),
)

#: (이름, 기대 키 SQL, 실측 키 SQL) — 슬롯 테이블은 키 집합까지 비교
_KEY_SETS: tuple[tuple[str, str, str], ...] = (
    (
        "actors",
        "SELECT DISTINCT actor_id FROM es.events"
        " WHERE world_id = %s AND type = 'actor.identity.declared'",
        "SELECT actor_id FROM read.actors WHERE world_id = %s",
    ),
    (
        "actor_arcs",
        "SELECT DISTINCT payload->>'target_actor_id' FROM es.events"
        " WHERE world_id = %s AND type = 'system.director.arc_planned'",
        "SELECT actor_id FROM read.actor_arcs WHERE world_id = %s",
    ),
)

_WORLDS_SQL = "SELECT DISTINCT world_id FROM es.events"


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
