"""부활 재사영 검증 — actor.identity.returned가 소멸된 read 모델을 되세운다.

원칙 (ADR-003): es는 불변, 복원도 이벤트다 — returned를 소비한 프로젝터가
es(SoT)에서 그 액터 범위(returned ULID 이전)를 기존 apply 경로에 다시 먹인다.
은퇴가 지운 것과 정확히 대칭이고, 라이브 소비와 from-es 리플레이가 같은
재사영 함수를 부르므로 결정적이다.

왕복 계약 (각 kind):
적재 → retired(소멸) → returned(부활 — 은퇴 전과 동등) → from-es 리플레이 2회
(동일 최종 상태·멱등) → 재은퇴(재소멸 — 부활 이후의 은퇴가 이긴다).
verify 3종은 "행 이후의 마지막 라이프사이클이 retired일 때만 소멸"로 판정한다.
"""

import json
import os

import httpx
import pytest
from lf_eventstore import NewEvent, append, current_head
from lf_eventstore.migrate import migrate
from lf_projector.config import Config
from lf_projector.kuzu_projector import KuzuProjector
from lf_projector.kuzu_verify import verify_worlds
from lf_projector.os_index import OpenSearchIndex, envelope_to_doc
from lf_projector.os_projector import reindex_returned
from lf_projector.pg_read import TABLES, ReadStore
from lf_projector.pg_verify import verify_pg
from lf_projector.redis_projector import RedisProjector
from lf_projector.replay import PATTERNS, RETURN_SCOPES, replay_into
from lf_projector.timeline import TimelineStore
from lf_projector.timeline_verify import verify_timeline

from .conftest import sample

WORLD = "w_main"
PLAYER = "p_observer_0417"
ARIA = "a_aria_kim"      # 은퇴했다 돌아오는 액터 (samples의 주인공)
JUNHO = "a_junho_park"   # 존속하는 액터 — 남의 역사의 증인


def _config(**overrides) -> Config:
    return Config(nats_url="", opensearch_url="", env="t", **overrides)


def eid(n: int, tag: str) -> str:
    """테스트용 ULID — 시간부(n)와 꼬리(tag)로 순서를 또렷하게 박는다.

    은퇴 샘플은 01JZK7Q3W2…R, 부활 샘플은 01JZK7Q3W4…T 다:
    n<2 는 은퇴 이전, n=6 은 부활 이후의 재은퇴 자리다.
    """
    assert len(tag) == 2
    return f"01JZK7Q3W{n}" + "0" * 14 + tag


def retire_envelope() -> dict:
    envelope = sample("actor.identity.retired")
    assert envelope["payload"]["actor_id"] == ARIA
    return envelope


def returned_envelope() -> dict:
    envelope = sample("actor.identity.returned")
    assert envelope["payload"]["actor_id"] == ARIA  # 계약: 재사영 키는 payload에 있다
    assert envelope["event_id"] > retire_envelope()["event_id"]  # 부활은 은퇴 뒤다
    return envelope


def re_retire_envelope() -> dict:
    """부활 이후의 재은퇴 — 더 큰 ULID라 되살린 것을 다시 지운다."""
    return dict(retire_envelope(), event_id=eid(6, "R9"), correlation_id=eid(6, "R9"))


# ── 순수 — 부활 범위 술어의 우주 ──────────────────────────────────────


def test_return_scopes_mirror_patterns():
    """kind별 부활 범위가 리플레이 술어와 같은 우주를 산다 — 넷 다 정의돼 있다."""
    assert set(RETURN_SCOPES) == set(PATTERNS)


# ── es 적재 헬퍼 (test_retire와 동일 규약) ────────────────────────────


async def _seed_es(pg) -> None:
    await pg.execute("DROP SCHEMA IF EXISTS es CASCADE")
    await migrate(pg)


async def _append_envelope(pg, principal: str, envelope: dict, *, stream_key=None) -> None:
    """es 적재 — head는 자동 재수화. event_id는 봉투의 것을 그대로 쓴다 (순서 고정)."""
    key = (
        stream_key or envelope.get("actor_id")
        or envelope["payload"].get("player_id") or "k"
    )
    head = await current_head(pg, envelope["world_id"], envelope["stream"], key)
    await append(
        pg, principal,
        [NewEvent(
            world_id=envelope["world_id"], stream=envelope["stream"], stream_key=key,
            type=envelope["type"], tick=envelope["tick"],
            actor_id=envelope.get("actor_id"), payload=envelope["payload"],
            event_id=envelope["event_id"],
        )],
        expected_head=head,
    )


def _rel(from_id: str, to_id: str) -> dict:
    envelope = json.loads(json.dumps(sample("relationship.state.changed")))
    envelope["payload"] = dict(envelope["payload"], from_id=from_id, to_id=to_id)
    return envelope


# ── kuzu — 노드+양방향 간선의 왕복 (라이브 경로 + from-es 리플레이) ────


async def test_kuzu_return_roundtrip_and_replay_deterministic(pg, tmp_path):
    await _seed_es(pg)
    rels = [_rel(ARIA, JUNHO), _rel(JUNHO, ARIA), _rel(JUNHO, PLAYER)]
    for i, envelope in enumerate(rels):
        envelope["event_id"] = eid(1, f"R{i}")
        p = envelope["payload"]
        await _append_envelope(
            pg, "engine.relationship", envelope, stream_key=f"{p['from_id']}|{p['to_id']}"
        )
    await _append_envelope(pg, "services.gateway", retire_envelope())
    await _append_envelope(pg, "services.gateway", returned_envelope())

    full = {(ARIA, JUNHO), (JUNHO, ARIA), (JUNHO, PLAYER)}
    projector = KuzuProjector(_config(kuzu_dir=str(tmp_path / "kuzu")))
    try:
        # 라이브 소비 순서 그대로: 관계 → 은퇴(소멸) → 부활(재사영)
        for envelope in rels:
            projector.project(envelope)
        projector.project(retire_envelope())
        assert projector.graph.all_edges(WORLD) == {(JUNHO, PLAYER)}
        assert await projector.reproject_returned(pg, returned_envelope()) == 2
        assert await projector.reproject_returned(pg, returned_envelope()) == 2  # 멱등
        assert projector.graph.all_edges(WORLD) == full
        assert (await verify_worlds(pg, projector.graph))["ok"]

        # from-es 리플레이 2회 — 같은 최종 상태 (returned도 같은 재사영 경로)
        for _ in range(2):
            fed = await replay_into(pg, PATTERNS["kuzu"], projector.replay_apply(pg))
            assert fed == 5
            assert projector.graph.all_edges(WORLD) == full
            assert (await verify_worlds(pg, projector.graph))["ok"]

        # 재은퇴 — 부활 이후의 은퇴가 이긴다 (되살린 것을 다시 지운다)
        await _append_envelope(pg, "services.gateway", re_retire_envelope())
        projector.project(re_retire_envelope())
        assert projector.graph.all_edges(WORLD) == {(JUNHO, PLAYER)}
        assert (await verify_worlds(pg, projector.graph))["ok"]
    finally:
        projector.graph.close()


# ── redis — 팔로워 인덱스·타임라인 발신분의 왕복 ──────────────────────


async def _redis_snapshot(redis) -> dict:
    """타임라인·인덱스·마커 키 전체의 정렬 덤프 — '은퇴 전과 동등'의 판정 기준."""
    snapshot: dict = {}
    async for raw in redis.scan_iter(match="lf:tl*"):
        key = raw.decode() if isinstance(raw, bytes) else raw
        if key.startswith("lf:tl:"):
            snapshot[key] = await redis.zrange(key, 0, -1)
        else:
            snapshot[key] = sorted(await redis.smembers(key))
    return snapshot


async def test_redis_return_roundtrip_and_replay_deterministic(pg, redis):
    await _seed_es(pg)
    rel = _rel(ARIA, PLAYER)  # stand-in 팔로우 + 변화 리시트(아리 발신)의 원천
    rel["event_id"] = eid(1, "RA")
    await _append_envelope(pg, "engine.relationship", rel, stream_key=f"{ARIA}|{PLAYER}")
    follow = sample("player.follow.changed")  # PLAYER → a_minji_kim (존속 인덱스)
    await _append_envelope(pg, "services.gateway", dict(follow, event_id=eid(1, "FW")))
    post = dict(sample("feed.post.published"), event_id=eid(1, "PA"), actor_id=ARIA)
    await _append_envelope(pg, "engine.feed", post)  # 아리 발신 → 팔로워 팬아웃
    reply = dict(sample("actor.message.sent"), event_id=eid(1, "MA"))
    await _append_envelope(pg, "engine.actor", reply)  # 아리 발신 → PLAYER 단독 배달

    projector = RedisProjector(_config())
    store = TimelineStore(redis)
    apply = projector.replay_apply(store, pg)  # 라이브와 같은 appliers (동일 경로)
    assert await replay_into(pg, PATTERNS["redis"], apply) == 4
    baseline = await _redis_snapshot(redis)
    assert await store.followers(WORLD, ARIA) == {PLAYER}
    assert len(await redis.zrange(store.timeline_key(WORLD, PLAYER), 0, -1)) == 3
    assert (await verify_timeline(pg, redis))["ok"]

    # 은퇴 — 인덱스·발신분이 걷힌다
    await _append_envelope(pg, "services.gateway", retire_envelope())
    await apply(retire_envelope())
    assert await store.followers(WORLD, ARIA) == set()
    assert await redis.zrange(store.timeline_key(WORLD, PLAYER), 0, -1) == []
    assert (await verify_timeline(pg, redis))["ok"]

    # 부활 — 은퇴 전과 동등, 재적용은 멱등
    await _append_envelope(pg, "services.gateway", returned_envelope())
    await apply(returned_envelope())
    assert await _redis_snapshot(redis) == baseline
    await apply(returned_envelope())
    assert await _redis_snapshot(redis) == baseline
    assert (await verify_timeline(pg, redis))["ok"]

    # from-es 리플레이 2회 — 같은 최종 상태·멱등
    for _ in range(2):
        fed = await replay_into(pg, PATTERNS["redis"], projector.replay_apply(store, pg))
        assert fed == 6
        assert await _redis_snapshot(redis) == baseline
        assert (await verify_timeline(pg, redis))["ok"]

    # 재은퇴 — 다시 걷힌다, 남의 인덱스는 남는다
    await _append_envelope(pg, "services.gateway", re_retire_envelope())
    await apply(re_retire_envelope())
    assert await store.followers(WORLD, ARIA) == set()
    assert await store.followers(WORLD, follow["payload"]["target_actor_id"]) == {PLAYER}
    assert await redis.zrange(store.timeline_key(WORLD, PLAYER), 0, -1) == []
    assert (await verify_timeline(pg, redis))["ok"]


# ── pg — 행·스레드·양방향 신념의 왕복 (PG 게이트) ─────────────────────


async def _pg_snapshot(pg) -> dict[str, list[str]]:
    """read 전 테이블의 정렬 덤프 — '은퇴 전과 동등'의 판정 기준."""
    snapshot: dict[str, list[str]] = {}
    for table in TABLES:
        rows = await (await pg.execute(f"SELECT * FROM {table}")).fetchall()
        snapshot[table] = sorted(repr(row) for row in rows)
    return snapshot


async def _load_pg_world(pg) -> None:
    """은퇴 소멸 규칙의 전 표면을 덮는 적재 — test_retire의 재료와 동일 구도.

    아리의 정체성·기억·신념(양방향)·아크·메시지·포스트 스레드가 모두 걸리고,
    준호의 삶과 플레이어의 역사는 남는 쪽 대조군이다.
    """
    post = sample("feed.post.published")
    reply = sample("actor.message.sent")
    comment = sample("player.comment.posted")
    belief = sample("actor.belief.formed")
    arc = sample("system.director.arc_planned")
    rows: list[tuple[str, dict]] = [
        ("engine.actor", sample("actor.identity.declared")),                   # 아리 정체성
        ("engine.actor", dict(sample("actor.identity.declared"),
                              event_id=eid(0, "DJ"), actor_id=JUNHO)),         # 준호 정체성
        ("engine.actor", sample("actor.memory.consolidated")),                 # 아리의 기억
        ("engine.actor", belief),                                              # 아리가 품은 신념
        ("engine.actor", dict(belief, event_id=eid(1, "BB"), actor_id=JUNHO,
                              payload=dict(belief["payload"], about_id=ARIA))),  # 준호→아리
        ("engine.actor", dict(belief, event_id=eid(1, "BC"), actor_id=JUNHO,
                              payload=dict(belief["payload"], about_id=None))),  # 준호 자신
        ("engine.feed", dict(post, event_id=eid(1, "PA"), actor_id=ARIA)),     # 아리의 P1
        ("engine.feed", dict(post, event_id=eid(1, "PB"), actor_id=JUNHO)),    # 준호의 P2
        ("engine.director", dict(arc, event_id=eid(1, "AR"),
                                 payload=dict(arc["payload"], target_actor_id=ARIA))),
        ("engine.director", dict(arc, event_id=eid(1, "AS"),
                                 payload=dict(arc["payload"], target_actor_id=JUNHO))),
        # P1(아리의 포스트) 스레드 — 은퇴가 통째로 지우고 부활이 통째로 되살린다
        ("services.gateway", dict(comment, event_id=eid(1, "CA"),
                                  payload=dict(comment["payload"], post_id=eid(1, "PA")))),
        ("engine.actor", dict(reply, event_id=eid(1, "CB"), actor_id=JUNHO,
                              payload=dict(reply["payload"], channel="comment",
                                           target_player_id=None, target_actor_id=ARIA,
                                           post_id=eid(1, "PA"), in_reply_to=eid(1, "PA")))),
        # P2(준호의 포스트) 스레드 — 아리가 단 댓글만 죽었다 되살아난다
        ("engine.actor", dict(reply, event_id=eid(1, "CC"),
                              payload=dict(reply["payload"], channel="comment",
                                           target_player_id=None, target_actor_id=JUNHO,
                                           post_id=eid(1, "PB"), in_reply_to=eid(1, "PB")))),
        ("services.gateway", dict(comment, event_id=eid(1, "CD"),
                                  payload=dict(comment["payload"], target_actor_id=JUNHO,
                                               post_id=eid(1, "PB")))),
        # DM — 아리의 답장은 죽었다 되살아나고, 플레이어의 말은 내내 남는다
        ("services.gateway", sample("player.dm.sent")),
        ("engine.actor", reply),
    ]
    for principal, envelope in rows:
        await _append_envelope(pg, principal, envelope)


async def _assert_pg_retired(pg) -> None:
    """재은퇴 후의 소멸 단면 — 남는 것은 준호의 삶과 플레이어의 역사뿐이다."""
    actors = {r[0] for r in await (await pg.execute(
        "SELECT actor_id FROM read.actors WHERE world_id = %s", (WORLD,)
    )).fetchall()}
    assert actors == {JUNHO}
    episodes = await (await pg.execute("SELECT count(*) FROM read.actor_episodes")).fetchone()
    assert episodes == (0,)
    beliefs = await (await pg.execute(
        "SELECT actor_id, about_id FROM read.actor_beliefs"
    )).fetchall()
    assert beliefs == [(JUNHO, "-")]
    survivors = {r[0] for r in await (await pg.execute(
        "SELECT event_id FROM read.messages"
    )).fetchall()}
    assert survivors == {eid(1, "CD"), sample("player.dm.sent")["event_id"]}


async def test_pg_return_roundtrip_and_replay_deterministic(pg):
    await _seed_es(pg)
    await _load_pg_world(pg)
    store = ReadStore(pg)
    await store.ensure()
    fed = await replay_into(pg, PATTERNS["pg"], store.apply)
    assert fed == 14  # 포스트 둘은 pg 술어 밖 (스레드 역추적은 es가 안다)
    baseline = await _pg_snapshot(pg)
    assert baseline["read.actors"]  # 판정 기준이 빈 세계면 왕복이 무의미하다
    assert (await verify_pg(pg))["ok"]

    # 은퇴 — 소멸 (규칙 전모는 test_retire가 고정한다, 여기선 왕복의 중간 단면)
    await _append_envelope(pg, "services.gateway", retire_envelope())
    assert await store.apply(retire_envelope())
    await _assert_pg_retired(pg)
    assert (await verify_pg(pg))["ok"]

    # 부활 — 은퇴 전과 동등, 재적용은 멱등
    await _append_envelope(pg, "services.gateway", returned_envelope())
    assert await store.apply(returned_envelope())
    assert await _pg_snapshot(pg) == baseline
    assert await store.apply(returned_envelope())
    assert await _pg_snapshot(pg) == baseline
    assert (await verify_pg(pg))["ok"]

    # from-es 리플레이 2회 — 파괴 후 재구축 + 겹침 리플레이 모두 같은 최종 상태
    await store.drop()
    await store.ensure()
    for _ in range(2):
        fed = await replay_into(pg, PATTERNS["pg"], store.apply)
        assert fed == 16  # 적재 14 + retired + returned
        assert await _pg_snapshot(pg) == baseline
        assert (await verify_pg(pg))["ok"]

    # 재은퇴 — 부활 이후의 은퇴가 이긴다 (되살린 것을 다시 지운다)
    await _append_envelope(pg, "services.gateway", re_retire_envelope())
    assert await store.apply(re_retire_envelope())
    await _assert_pg_retired(pg)
    assert (await verify_pg(pg))["ok"]


async def test_verify_detects_unapplied_return(pg):
    """원천에는 부활이 있는데 프로젝션이 재사영하지 않았다 — verify가 드리프트로 잡는다."""
    await _seed_es(pg)
    await _append_envelope(pg, "engine.actor", sample("actor.identity.declared"))
    await _append_envelope(pg, "services.gateway", retire_envelope())
    await _append_envelope(pg, "services.gateway", returned_envelope())

    store = ReadStore(pg)
    await store.ensure()
    await store.apply(sample("actor.identity.declared"))
    await store.apply(retire_envelope())  # 부활은 반영하지 않았다

    report = await verify_pg(pg, world_id=WORLD)
    assert not report["ok"]
    assert report["worlds"][WORLD]["tables"]["actors"]["missing"] == [ARIA]


# ── os — 실색인 통합 (LF_TEST_OPENSEARCH_URL 있을 때만, 없으면 skip) ────

OS_URL = os.environ.get("LF_TEST_OPENSEARCH_URL")


@pytest.mark.skipif(OS_URL is None, reason="LF_TEST_OPENSEARCH_URL 미설정 — OpenSearch 통합 스킵")
async def test_os_reindex_returned_restores_actor_docs(pg):
    """delete_by_actor가 지운 작성자 문서만 es에서 되돌아온다 — 남의 글은 그대로다."""
    await _seed_es(pg)
    post = sample("feed.post.published")
    own = dict(post, event_id=eid(1, "PA"), actor_id=ARIA)
    others = dict(post, event_id=eid(1, "PB"), actor_id=JUNHO,
                  payload=dict(post["payload"], participants=[ARIA]))
    await _append_envelope(pg, "engine.feed", own)
    await _append_envelope(pg, "engine.feed", others)
    await _append_envelope(pg, "services.gateway", retire_envelope())
    await _append_envelope(pg, "services.gateway", returned_envelope())

    index = OpenSearchIndex(OS_URL, "lf-feed-posts-return-test")
    try:
        await index.drop()
        await index.ensure()
        await index.bulk_upsert([envelope_to_doc(own), envelope_to_doc(others)])
        assert await index.delete_by_actor(WORLD, ARIA) == 1

        assert await reindex_returned(index, pg, returned_envelope()) == 1
        assert await reindex_returned(index, pg, returned_envelope()) == 1  # upsert 멱등
        await index.refresh()
        async with httpx.AsyncClient(base_url=OS_URL.rstrip("/")) as client:
            r = await client.post(
                "/lf-feed-posts-return-test/_search", json={"query": {"match_all": {}}}
            )
            r.raise_for_status()
            hits = r.json()["hits"]["hits"]
            assert sorted(h["_id"] for h in hits) == [eid(1, "PA"), eid(1, "PB")]
    finally:
        await index.drop()
        await index.close()
