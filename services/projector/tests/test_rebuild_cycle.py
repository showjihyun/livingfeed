"""주간 배치의 전체 사슬 통합 검증 — verify → 드리프트 → rebuild --once → verify.

실 인프라(PG es/outbox + relay + JetStream)로 세 프로젝터의 운영 시나리오를
그대로 돌린다: 따라잡기(once)가 스스로 끝나고, verify가 고아 프로젝션을
잡아내고, 일회성 재구축이 원천과의 정합을 되찾는다 (ADR-003 계약 3).
종료는 깨끗해야 배치다 — drain 타임아웃(30s 대기 + ERROR 로그)은 결함이다.
"""

import logging

from lf_dispatcher.relay import relay_once
from lf_eventstore import NewEvent, Provenance, append
from lf_eventstore.migrate import migrate
from lf_projector.config import Config
from lf_projector.graph import RelGraph
from lf_projector.kuzu_projector import KuzuProjector
from lf_projector.kuzu_verify import verify_worlds
from lf_projector.pg_projector import PgProjector
from lf_projector.pg_read import ReadStore
from lf_projector.pg_verify import verify_pg
from lf_projector.redis_projector import RedisProjector
from lf_projector.timeline import TimelineStore
from lf_projector.timeline_verify import verify_timeline

from .conftest import NATS_URL, PG_DSN, REDIS_URL, sample


def _config(env: str, **overrides) -> Config:
    return Config(
        nats_url=NATS_URL, opensearch_url="http://unused", env=env,
        database_url=PG_DSN, redis_url=REDIS_URL, fetch_timeout_s=0.5,
        **overrides,
    )


async def _seed_es(pg) -> None:
    await pg.execute("DROP SCHEMA IF EXISTS es CASCADE")
    await migrate(pg)


async def test_pg_rebuild_cycle_restores_integrity(pg, js, caplog):
    await _seed_es(pg)
    identity = sample("actor.identity.declared")
    await append(
        pg, "engine.actor",
        [NewEvent(
            world_id=identity["world_id"], stream="actor", stream_key=identity["actor_id"],
            type=identity["type"], tick=identity["tick"], actor_id=identity["actor_id"],
            payload=identity["payload"],
            provenance=Provenance.from_json(identity["provenance"]),
        )],
        expected_head=0,
    )
    await relay_once(pg, js, "trbpg")

    cfg = _config("trbpg", pg_durable="trbpg-pg")
    with caplog.at_level(logging.ERROR, logger="nats.aio.client"):
        await PgProjector(cfg).run(once=True)  # 따라잡기 — 유휴에서 스스로 끝난다
    assert not caplog.records  # drain 타임아웃 없이 즉시 깨끗하게 종료해야 한다
    assert (await verify_pg(pg))["ok"]

    # 고아 세계 드리프트 주입 — 원천에 없는 세계의 read 행
    await ReadStore(pg).apply({**identity, "world_id": "w_ghost"})
    report = await verify_pg(pg)
    assert not report["ok"]
    assert report["worlds"]["w_ghost"]["tables"]["actors"]["extra"] == [identity["actor_id"]]

    # 일회성 재구축 배치 — 잔여물을 지우고 원천에서 다시 세운다
    await PgProjector(cfg).run(rebuild=True, once=True)
    report = await verify_pg(pg)
    assert report["ok"]
    assert "w_ghost" not in report["worlds"]


async def test_redis_rebuild_cycle_restores_integrity(pg, redis, js):
    await _seed_es(pg)
    follow = sample("player.follow.changed")
    await append(
        pg, "services.gateway",
        [NewEvent(
            world_id=follow["world_id"], stream="player",
            stream_key=follow["payload"]["player_id"], type=follow["type"],
            tick=follow["tick"], payload=follow["payload"],
            provenance=Provenance.from_json(follow["provenance"]),
        )],
        expected_head=0,
    )
    await relay_once(pg, js, "trbrd")

    cfg = _config("trbrd", redis_durable="trbrd-redis")
    await RedisProjector(cfg).run(once=True)
    assert (await verify_timeline(pg, redis))["ok"]

    await TimelineStore(redis).set_follow("w_ghost", "a_ghost", "p_ghost", True)
    assert not (await verify_timeline(pg, redis))["ok"]

    await RedisProjector(cfg).run(rebuild=True, once=True)
    report = await verify_timeline(pg, redis)
    assert report["ok"]
    assert "w_ghost" not in report["worlds"]


async def test_kuzu_rebuild_cycle_restores_integrity(pg, js, tmp_path):
    await _seed_es(pg)
    rel = sample("relationship.state.changed")
    payload = rel["payload"]
    await append(
        pg, "engine.relationship",
        [NewEvent(
            world_id=rel["world_id"], stream="relationship",
            stream_key=f"{payload['from_id']}|{payload['to_id']}", type=rel["type"],
            tick=rel["tick"], actor_id=payload["from_id"], payload=payload,
            provenance=Provenance.from_json(rel["provenance"]),
        )],
        expected_head=0,
    )
    await relay_once(pg, js, "trbkz")

    cfg = _config("trbkz", kuzu_dir=str(tmp_path / "kuzu"), kuzu_durable="trbkz-kuzu")
    await KuzuProjector(cfg).run(once=True)  # once가 graph API까지 함께 내린다

    graph = RelGraph(tmp_path / "kuzu")
    try:
        assert (await verify_worlds(pg, graph))["ok"]
        graph.apply_state_changed("w_ghost", rel)  # 고아 세계 드리프트
        assert not (await verify_worlds(pg, graph))["ok"]
    finally:
        graph.close()

    await KuzuProjector(cfg).run(rebuild=True, once=True)  # drop_all + 재소비
    graph = RelGraph(tmp_path / "kuzu")
    try:
        report = await verify_worlds(pg, graph)
        assert report["ok"]
        assert "w_ghost" not in report["worlds"]  # 재구축이 잔여물까지 지웠다
    finally:
        graph.close()
