"""Director 개입 적재 통합 — 감사 선행·인과 연결·권한 (ADR-013).

PostgreSQL 필요 (없으면 skip — conftest 참고). 관찰 루프(JetStream)는
E2E 스모크가 검증한다 — 여기는 evaluate의 적재 계약이다.
"""

from lf_director.config import Config
from lf_director.director import Director
from lf_director.signals import Snapshot
from lf_eventstore import read_stream

WORLD = "w_test"


def make_director(quiet_override: int | None = None) -> Director:
    cfg = Config(
        pg_dsn="unused", nats_url="unused", env="test", world_id=WORLD,
        quiet_ticks_override=quiet_override,
    )
    return Director(cfg)


async def test_intervention_appends_audit_then_incident(conn):
    director = make_director()
    fired = await director.evaluate(
        conn, Snapshot(tick=120, drama_ma=0.05, quiet_ticks=30), graph=None
    )
    assert fired

    [audit] = [s.envelope for s in await read_stream(conn, WORLD, "system", "director")]
    assert audit["type"] == "system.director.intervened"
    assert audit["payload"]["tool"] == "inject_incident"
    assert "침체 감지" in audit["payload"]["reason"]

    [incident] = [s.envelope for s in await read_stream(conn, WORLD, "world", "incidents")]
    assert incident["type"] == "world.incident.occurred"
    # 산출물은 감사 기록을 가리킨다 — 서사가 감사 가능 (ADR-002/013)
    assert incident["causation_id"] == audit["event_id"]
    assert incident["correlation_id"] == audit["event_id"]
    assert 0 < incident["payload"]["intensity"] <= 1


async def test_below_threshold_means_no_intervention(conn):
    director = make_director()
    fired = await director.evaluate(
        conn, Snapshot(tick=120, drama_ma=0.5, quiet_ticks=0), graph=None
    )
    assert not fired
    assert await read_stream(conn, WORLD, "world", "incidents") == []


async def test_budget_limits_consecutive_interventions(conn):
    director = make_director()
    fired = 0
    for i in range(5):
        if await director.evaluate(
            conn, Snapshot(tick=120 + i, drama_ma=0.05, quiet_ticks=30 + i), graph=None
        ):
            fired += 1
    assert fired == 2  # 창(세계 1시간)당 상한 (hard rule)
    incidents = await read_stream(conn, WORLD, "world", "incidents")
    assert len(incidents) == 2
