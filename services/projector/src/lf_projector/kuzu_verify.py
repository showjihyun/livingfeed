"""kuzu 그래프 무결성 검사 — 원천(es relationship 스트림) 대비 (ADR-006 후속).

프로젝션은 소모품이다 (ADR-003 계약 3): 이 검사는 아무것도 고치지 않고
어긋남만 보고한다 — 리포트가 --rebuild 판단의 근거다. 주간 배치로 돌리는
것을 상정한 체크 커맨드 (`--kind kuzu --verify`).

기대 집합: es의 relationship 스트림 키("from|to") 전부 — state.changed와
milestone 어느 쪽이든 첫 이벤트가 그래프 엣지를 만든다 (graph.py HANDLERS).
비교는 엣지 존재 집합 수준이다 — 차원 값의 드리프트 검증은 후속.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg import AsyncConnection

from lf_projector.graph import RelGraph

logger = logging.getLogger("lf.projector.kuzu_verify")

#: read-only 원천 질의 — lf_eventstore에 스트림 키 열거 API가 없어 직접 SELECT한다
_EXPECTED_SQL = (
    "SELECT DISTINCT stream_key FROM es.events "
    "WHERE world_id = %s AND stream = 'relationship'"
)
_WORLDS_SQL = "SELECT DISTINCT world_id FROM es.events WHERE stream = 'relationship'"


def compare(expected: set[tuple[str, str]], actual: set[tuple[str, str]]) -> dict[str, Any]:
    """기대(원천) vs 실측(그래프) — 순수 비교. missing이 프로젝션 누락이다."""
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    return {
        "ok": not missing and not extra,
        "expected": len(expected),
        "actual": len(actual),
        "missing": [f"{a}|{b}" for a, b in missing],
        "extra": [f"{a}|{b}" for a, b in extra],
    }


async def expected_edges(conn: AsyncConnection, world_id: str) -> set[tuple[str, str]]:
    """원천에서 파생되는 기대 엣지 집합 — relationship 스트림 키가 곧 방향 엣지다."""
    rows = await (await conn.execute(_EXPECTED_SQL, (world_id,))).fetchall()
    edges: set[tuple[str, str]] = set()
    for (stream_key,) in rows:
        from_id, _, to_id = stream_key.partition("|")
        if to_id:  # 형식 밖 키는 엣지가 아니다 (전방 호환 무시)
            edges.add((from_id, to_id))
    return edges


async def verify_worlds(
    conn: AsyncConnection, graph: RelGraph, *, world_id: str | None = None
) -> dict[str, Any]:
    """세계별 무결성 리포트 — world_id를 주면 그 세계만, 아니면 원천의 전 세계."""
    if world_id is not None:
        worlds = [world_id]
    else:
        rows = await (await conn.execute(_WORLDS_SQL)).fetchall()
        worlds = sorted(w for (w,) in rows)

    reports: dict[str, dict[str, Any]] = {}
    for world in worlds:
        report = compare(await expected_edges(conn, world), graph.all_edges(world))
        reports[world] = report
        logger.info(
            "무결성 %s — world=%s expected=%d actual=%d missing=%d extra=%d",
            "OK" if report["ok"] else "MISMATCH", world,
            report["expected"], report["actual"],
            len(report["missing"]), len(report["extra"]),
        )
        if not report["ok"]:
            logger.warning(
                "어긋남 상세 — world=%s missing=%s extra=%s (고치려면 --rebuild)",
                world, report["missing"][:10], report["extra"][:10],
            )
    return {"ok": all(r["ok"] for r in reports.values()), "worlds": reports}
