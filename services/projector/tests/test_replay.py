"""es(SoT) 기반 완전 재구축 검증 (ADR-003 계약 3 완결).

핵심 계약 둘:
1. 리플레이 봉투 = relay가 발행했을 봉투 (outbox 원본과 동일) — 프로젝터가
   NATS에서 받든 es에서 받든 같은 것을 본다.
2. NATS를 전혀 거치지 않은 이벤트(보존 한도 밖 시나리오)도 from-es 재구축이
   프로젝션을 원천과 정합하게 되세운다 — verify 3종이 그 증인이다.
"""

from lf_eventstore import NewEvent, append
from lf_eventstore.migrate import migrate
from lf_projector.config import Config
from lf_projector.kuzu_projector import KuzuProjector
from lf_projector.kuzu_verify import verify_worlds
from lf_projector.pg_read import ReadStore
from lf_projector.pg_verify import verify_pg
from lf_projector.redis_projector import RedisProjector
from lf_projector.replay import PATTERNS, replay_envelopes, replay_into, split_patterns
from lf_projector.timeline import TimelineStore
from lf_projector.timeline_verify import verify_timeline

from .conftest import sample


def _config(**overrides) -> Config:
    return Config(nats_url="", opensearch_url="", env="t", **overrides)


# ── 단위 — 패턴 분해 (NATS filter subject와 동형) ─────────────────────


def test_split_patterns_separates_prefix_and_exact():
    likes, exacts = split_patterns(("actor.>", "player.follow.changed", "system.>"))
    assert likes == ["actor.%", "system.%"]
    assert exacts == ["player.follow.changed"]
    assert split_patterns(()) == ([], [])


def test_patterns_mirror_consumer_filters():
    # kind별 술어가 각 프로젝터의 NATS filter와 동형이어야 정직한 리플레이다
    assert PATTERNS["kuzu"] == ("relationship.>", "actor.identity.retired")
    assert PATTERNS["os"] == ("feed.post.published", "actor.identity.retired")
    assert set(PATTERNS["pg"]) == {"actor.>", "player.>", "system.>"}
    assert "relationship.>" in PATTERNS["redis"]
    assert "player.follow.changed" in PATTERNS["redis"]
    # 은퇴 소멸은 pg 밖 세 kind가 별도 소스로 듣는다 (pg는 actor.> 가 이미 덮는다)
    assert "actor.identity.retired" in PATTERNS["redis"]


# ── 통합 — 봉투 동일성 + 순서 + kind별 from-es 사이클 (NATS 불요) ──────


async def _seed_es(pg) -> None:
    await pg.execute("DROP SCHEMA IF EXISTS es CASCADE")
    await migrate(pg)


async def _append_sample(pg, principal: str, envelope: dict, *, expected_head: int) -> None:
    key = envelope.get("actor_id") or envelope["payload"].get("player_id") or "k"
    await append(
        pg, principal,
        [NewEvent(
            world_id=envelope["world_id"], stream=envelope["stream"],
            stream_key=key, type=envelope["type"], tick=envelope["tick"],
            actor_id=envelope.get("actor_id"), payload=envelope["payload"],
        )],
        expected_head=expected_head,
    )


async def test_replayed_envelope_equals_outbox_original(pg):
    """es 행에서 재구성한 봉투가 relay 발행분(outbox 원본)과 완전히 같다."""
    await _seed_es(pg)
    await _append_sample(pg, "engine.actor", sample("actor.identity.declared"), expected_head=0)
    cur = await pg.execute("SELECT envelope FROM es.outbox ORDER BY global_seq")
    [(original,)] = await cur.fetchall()

    replayed = [e async for e in replay_envelopes(pg, ("actor.>",))]
    assert replayed == [original]


async def test_replay_filters_and_keeps_global_order(pg):
    await _seed_es(pg)
    identity = sample("actor.identity.declared")
    rel = sample("relationship.state.changed")
    memory = sample("actor.memory.consolidated")
    await _append_sample(pg, "engine.actor", identity, expected_head=0)
    await append(
        pg, "engine.relationship",
        [NewEvent(
            world_id=rel["world_id"], stream="relationship",
            stream_key=f"{rel['payload']['from_id']}|{rel['payload']['to_id']}",
            type=rel["type"], tick=rel["tick"], payload=rel["payload"],
        )],
        expected_head=0,
    )
    await _append_sample(pg, "engine.actor", memory, expected_head=1)

    types = [e["type"] async for e in replay_envelopes(pg, PATTERNS["pg"], batch_size=1)]
    # relationship은 걸러지고, actor 둘은 적재 순서(global_seq) 그대로다
    assert types == ["actor.identity.declared", "actor.memory.consolidated"]


async def test_pg_from_es_rebuild_restores_integrity(pg):
    """NATS를 전혀 거치지 않은 원천 → from-es 리플레이 → verify 무결."""
    await _seed_es(pg)
    await _append_sample(pg, "engine.actor", sample("actor.identity.declared"), expected_head=0)
    await _append_sample(
        pg, "engine.actor", sample("actor.memory.consolidated"), expected_head=1
    )
    arc = sample("system.director.arc_planned")
    await append(
        pg, "engine.director",
        [NewEvent(
            world_id=arc["world_id"], stream="system", stream_key="arc",
            type=arc["type"], tick=arc["tick"], payload=arc["payload"],
        )],
        expected_head=0,
    )

    store = ReadStore(pg)
    await store.ensure()
    assert not (await verify_pg(pg))["ok"]  # 아직 프로젝션이 비어 있다 — 어긋남

    fed = await replay_into(pg, PATTERNS["pg"], store.apply)
    assert fed == 3
    assert (await verify_pg(pg))["ok"]


async def test_redis_from_es_rebuild_restores_integrity(pg, redis):
    await _seed_es(pg)
    follow = sample("player.follow.changed")
    declare = {**follow["payload"], "following": True}
    withdraw = {**follow["payload"], "following": False}
    for head, payload in ((0, declare), (1, withdraw)):
        await append(
            pg, "services.gateway",
            [NewEvent(
                world_id=follow["world_id"], stream="player",
                stream_key=payload["player_id"], type=follow["type"],
                tick=follow["tick"] + head, payload=payload,
            )],
            expected_head=head,
        )

    store = TimelineStore(redis)
    # 드리프트: 선언만 반영된 낡은 인덱스 — 철회가 리플레이로 이겨야 한다
    await store.set_follow(
        follow["world_id"], declare["target_actor_id"], declare["player_id"], True
    )
    assert not (await verify_timeline(pg, redis))["ok"]

    fed = await replay_into(
        pg, PATTERNS["redis"], RedisProjector(_config()).replay_apply(store)
    )
    assert fed == 2
    assert (await verify_timeline(pg, redis))["ok"]


async def test_kuzu_from_es_rebuild_restores_integrity(pg, tmp_path):
    await _seed_es(pg)
    rel = sample("relationship.state.changed")
    payload = rel["payload"]
    reverse = {**payload, "from_id": payload["to_id"], "to_id": payload["from_id"]}
    for p in (payload, reverse):
        await append(
            pg, "engine.relationship",
            [NewEvent(
                world_id=rel["world_id"], stream="relationship",
                stream_key=f"{p['from_id']}|{p['to_id']}", type=rel["type"],
                tick=rel["tick"], payload=p,
            )],
            expected_head=0,
        )

    projector = KuzuProjector(_config(kuzu_dir=str(tmp_path / "kuzu")))
    try:
        assert not (await verify_worlds(pg, projector.graph))["ok"]  # 빈 그래프 — 어긋남
        fed = await replay_into(pg, PATTERNS["kuzu"], projector.replay_apply())
        assert fed == 2
        assert (await verify_worlds(pg, projector.graph))["ok"]
    finally:
        projector.graph.close()
