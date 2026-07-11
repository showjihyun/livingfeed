"""SQL 마이그레이션 러너 — es.schema_migrations 로 적용 이력을 추적한다.

실행:
    uv run --package lf-eventstore python -m lf_eventstore.migrate <DSN>

compose initdb는 스키마 생성만 담당하고(01-schemas.sql), 테이블 정의는
전부 이 러너를 통해 적용한다 — dev/CI/prod가 같은 경로를 탄다 (ADR-019).
"""

from __future__ import annotations

import asyncio
import sys
from importlib.resources import files

from psycopg import AsyncConnection

_MIGRATIONS = files("lf_eventstore") / "migrations"


def migration_files() -> list[tuple[str, str]]:
    """(이름, SQL 본문) 목록 — 이름 순으로 적용된다."""
    entries = [
        (f.name, f.read_text(encoding="utf-8"))
        for f in _MIGRATIONS.iterdir()
        if f.name.endswith(".sql")
    ]
    return sorted(entries)


async def migrate(conn: AsyncConnection) -> list[str]:
    """미적용 마이그레이션을 순서대로 적용하고 적용된 이름 목록을 반환한다."""
    async with conn.transaction():
        await conn.execute("CREATE SCHEMA IF NOT EXISTS es")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS es.schema_migrations (
                name        TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )

    applied: list[str] = []
    for name, sql in migration_files():
        async with conn.transaction():
            # 동시 실행 보호 — 같은 마이그레이션을 두 러너가 적용하지 못하게 직렬화
            await conn.execute("SELECT pg_advisory_xact_lock(7420250711)")
            cur = await conn.execute(
                "SELECT 1 FROM es.schema_migrations WHERE name = %s", (name,)
            )
            if await cur.fetchone() is not None:
                continue
            await conn.execute(sql)
            await conn.execute("INSERT INTO es.schema_migrations (name) VALUES (%s)", (name,))
            applied.append(name)
    return applied


async def _main(dsn: str) -> int:
    async with await AsyncConnection.connect(dsn) as conn:
        applied = await migrate(conn)
    for name in applied:
        print(f"applied: {name}")
    if not applied:
        print("최신 상태 — 적용할 마이그레이션 없음")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("사용법: python -m lf_eventstore.migrate <DSN>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(_main(sys.argv[1])))
