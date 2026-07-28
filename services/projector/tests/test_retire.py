"""은퇴 소멸 검증 — actor.identity.retired를 read 모델 4종이 집행한다.

원칙 (ADR-003): es는 불변(역사 보존), 삭제는 이벤트다 — 프로젝터가 소비해
소멸을 집행하고, 리플레이(--rebuild --from-es)해도 같은 소멸이 재생성된다.

무엇을 지우고 무엇을 남기나:
- pg:    그 액터의 행(identity·episodes·beliefs·arcs) + ① 그가 쓴 메시지
         ② 그의 포스트에 달린 스레드 전체 (고아 댓글 방지 — es 역추적).
         플레이어가 쓴 DM·남의 포스트 스레드는 남는다 (남의 역사)
- kuzu:  노드 + 양방향 간선 — 상대 노드와 상대의 다른 관계는 남는다
- redis: 팔로워 인덱스·거부 마커 + 타임라인의 그 액터 발신 엔트리
- os:    actor_id(작성자) 일치 문서만 — participants에만 낀 남의 포스트는 남는다
"""

import json
import os

import httpx
import pytest
from lf_eventstore import NewEvent, Provenance, append, current_head
from lf_eventstore.migrate import migrate
from lf_projector.config import Config
from lf_projector.graph import RelGraph
from lf_projector.kuzu_projector import KuzuProjector
from lf_projector.kuzu_verify import verify_worlds
from lf_projector.os_index import OpenSearchIndex, envelope_to_doc, retire_query
from lf_projector.pg_read import ReadStore
from lf_projector.pg_verify import verify_pg
from lf_projector.redis_projector import RedisProjector
from lf_projector.replay import PATTERNS, replay_into
from lf_projector.timeline import TimelineStore
from lf_projector.timeline_verify import fold_followers, verify_timeline

from .conftest import sample

WORLD = "w_main"
PLAYER = "p_observer_0417"
ARIA = "a_aria_kim"      # 은퇴하는 액터 (samples의 주인공)
JUNHO = "a_junho_park"   # 존속하는 액터 — 남의 역사의 증인


def _config(**overrides) -> Config:
    return Config(nats_url="", opensearch_url="", env="t", **overrides)


def eid(n: int, tag: str) -> str:
    """테스트용 ULID — 시간부(n)와 꼬리(tag)로 순서를 또렷하게 박는다.

    은퇴 샘플은 01JZK7Q3W2…R 이다: n<2 는 은퇴 이전, n>2 는 은퇴 이후.
    """
    assert len(tag) == 2
    return f"01JZK7Q3W{n}" + "0" * 14 + tag


def retire_envelope() -> dict:
    envelope = sample("actor.identity.retired")
    assert envelope["payload"]["actor_id"] == ARIA  # 계약: 소멸 키는 payload에 있다
    return envelope


# ── 순수 — os 소멸 질의의 표적 ────────────────────────────────────────


def test_retire_query_targets_author_only():
    """소멸 질의는 작성자(actor_id)만 겨눈다 — participants에 낀 남의 글은 남는다."""
    q = retire_query(WORLD, ARIA)
    assert q["query"]["bool"]["filter"] == [
        {"term": {"world_id": WORLD}},
        {"term": {"actor_id": ARIA}},
    ]


def test_fold_followers_excludes_retired_actor():
    """기대 팔로워 fold — 은퇴 액터는 stand-in·명시 선언 어느 쪽으로도 남지 않는다."""
    rel_keys = [f"{ARIA}|{PLAYER}", f"{JUNHO}|{PLAYER}"]
    follows = [{"player_id": PLAYER, "target_actor_id": ARIA, "following": True}]
    assert fold_followers(rel_keys, follows, {ARIA}) == {JUNHO: {PLAYER}}
    assert fold_followers(rel_keys, follows) != {JUNHO: {PLAYER}}  # 기본값은 무은퇴


# ── kuzu — 노드 + 양방향 간선 (임베디드라 인프라 불요) ─────────────────


def _rel(from_id: str, to_id: str) -> dict:
    envelope = json.loads(json.dumps(sample("relationship.state.changed")))
    envelope["payload"] = dict(envelope["payload"], from_id=from_id, to_id=to_id)
    return envelope


def test_kuzu_retire_deletes_node_and_both_direction_edges(tmp_path):
    graph = RelGraph(tmp_path / "kuzu")
    try:
        graph.apply_state_changed(WORLD, _rel(ARIA, JUNHO))
        graph.apply_state_changed(WORLD, _rel(JUNHO, ARIA))
        graph.apply_state_changed(WORLD, _rel(JUNHO, PLAYER))

        graph.apply_retired(WORLD, retire_envelope())
        graph.apply_retired(WORLD, retire_envelope())  # 재적용 — 멱등 (계약 1)

        # 양방향 간선이 모두 사라지고, 준호의 다른 관계는 온전하다
        assert graph.all_edges(WORLD) == {(JUNHO, PLAYER)}
        [edge] = graph.player_graph(WORLD, PLAYER)["edges"]
        assert edge["actor_id"] == JUNHO
        # 노드도 사라졌다 — 세계 관계망에 아리의 흔적이 없다
        assert ARIA not in graph.world_graph(WORLD, min_weight=0.0)["nodes"]
    finally:
        graph.close()


def test_kuzu_projector_routes_retirement(tmp_path):
    """HANDLERS 배선 — project()가 은퇴 봉투를 소멸로 라우팅한다 (리플레이 동일 경로)."""
    projector = KuzuProjector(_config(kuzu_dir=str(tmp_path / "kuzu")))
    try:
        projector.project(_rel(ARIA, JUNHO))
        projector.project(retire_envelope())
        assert projector.graph.all_edges(WORLD) == set()
    finally:
        projector.graph.close()


# ── redis — 팔로워 인덱스·거부 마커·타임라인 발신분 ───────────────────


async def test_redis_retire_clears_index_markers_and_authored_entries(redis):
    store = TimelineStore(redis)
    await store.set_follow(WORLD, ARIA, PLAYER, True)
    await store.set_follow(WORLD, JUNHO, PLAYER, True)
    await store.set_follow(WORLD, ARIA, "p_other", False)  # 거부 마커도 걷힌다
    await store.push_reply(sample("actor.message.sent"))  # 아리 발신 → PLAYER 타임라인
    junho_reply = dict(sample("actor.message.sent"), event_id=eid(1, "4J"), actor_id=JUNHO)
    await store.push_reply(junho_reply)

    assert await store.retire_actor(WORLD, ARIA) == 1
    assert await store.followers(WORLD, ARIA) == set()
    assert await redis.exists(store.unfollow_key(WORLD, ARIA)) == 0
    assert await store.followers(WORLD, JUNHO) == {PLAYER}  # 남의 인덱스는 남는다

    raw = await redis.zrange(store.timeline_key(WORLD, PLAYER), 0, -1)
    assert [json.loads(r)["actor_id"] for r in raw] == [JUNHO]  # 남의 발신분은 남는다
    assert await store.retire_actor(WORLD, ARIA) == 0  # 재실행 — 무연산 (멱등)


# ── pg — 행 소멸·스레드 고아 방지·ULID 가드 (PG 게이트) ────────────────


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
            provenance=Provenance.from_json(envelope["provenance"]),
        )],
        expected_head=head,
    )


async def test_pg_retire_wipes_actor_rows_and_threads_preserves_others(pg):
    """소멸 규칙의 전모 — 자기 행·자기 메시지·자기 포스트 스레드만 죽는다."""
    await _seed_es(pg)  # es(SoT)가 그의 포스트 post_id 집합의 원천이다
    store = ReadStore(pg)
    await store.ensure()

    # es: 두 포스트 — 아리의 P1, 준호의 P2 (스레드 역추적 재료)
    post = sample("feed.post.published")
    p1 = dict(post, event_id=eid(1, "PA"), actor_id=ARIA)
    p2 = dict(post, event_id=eid(1, "PB"), actor_id=JUNHO)
    await _append_envelope(pg, "engine.feed", p1)
    await _append_envelope(pg, "engine.feed", p2)

    # read 상태: 두 삶과 대화들
    reply = sample("actor.message.sent")
    comment = sample("player.comment.posted")
    belief = sample("actor.belief.formed")
    arc = sample("system.director.arc_planned")
    rows = [
        sample("actor.identity.declared"),                                    # 아리 정체성
        dict(sample("actor.identity.declared"), event_id=eid(0, "DJ"),
             actor_id=JUNHO),                                                 # 준호 정체성
        sample("actor.memory.consolidated"),                                  # 아리의 기억
        belief,                                                               # 아리가 품은 신념
        dict(belief, event_id=eid(1, "BB"), actor_id=JUNHO,
             payload=dict(belief["payload"], about_id=ARIA)),                 # 준호→아리 신념
        dict(belief, event_id=eid(1, "BC"), actor_id=JUNHO,
             payload=dict(belief["payload"], about_id=None)),                 # 준호 자신의 신념
        dict(arc, event_id=eid(1, "AR"),
             payload=dict(arc["payload"], target_actor_id=ARIA)),             # 아리의 아크
        dict(arc, event_id=eid(1, "AS"),
             payload=dict(arc["payload"], target_actor_id=JUNHO)),            # 준호의 아크
        # P1(아리의 포스트) 스레드 — 통째로 죽는다 (고아 방지)
        dict(comment, event_id=eid(1, "CA"),
             payload=dict(comment["payload"], post_id=p1["event_id"])),
        dict(reply, event_id=eid(1, "CB"), actor_id=JUNHO,
             payload=dict(reply["payload"], channel="comment", target_player_id=None,
                          target_actor_id=ARIA, post_id=p1["event_id"],
                          in_reply_to=p1["event_id"])),
        # P2(준호의 포스트) 스레드 — 아리가 단 댓글(①)만 죽고 나머지는 남는다
        dict(reply, event_id=eid(1, "CC"),
             payload=dict(reply["payload"], channel="comment", target_player_id=None,
                          target_actor_id=JUNHO, post_id=p2["event_id"],
                          in_reply_to=p2["event_id"])),
        dict(comment, event_id=eid(1, "CD"),
             payload=dict(comment["payload"], target_actor_id=JUNHO,
                          post_id=p2["event_id"])),
        # DM — 아리의 답장(①)은 죽고, 플레이어가 쓴 말은 플레이어의 역사라 남는다
        sample("player.dm.sent"),
        reply,
    ]
    for envelope in rows:
        assert await store.apply(envelope)

    assert await store.apply(retire_envelope())
    assert await store.apply(retire_envelope())  # 재적용 — 멱등 (계약 1)

    actors = {r[0] for r in await (await pg.execute(
        "SELECT actor_id FROM read.actors WHERE world_id = %s", (WORLD,)
    )).fetchall()}
    assert actors == {JUNHO}

    episodes = await (await pg.execute("SELECT count(*) FROM read.actor_episodes")).fetchone()
    assert episodes == (0,)  # 아리의 기억뿐이었다

    beliefs = await (await pg.execute(
        "SELECT actor_id, about_id FROM read.actor_beliefs"
    )).fetchall()
    assert beliefs == [(JUNHO, "-")]  # 아리를 향한 신념도 함께 소멸 (매달린 참조 방지)

    arcs = {r[0] for r in await (await pg.execute(
        "SELECT actor_id FROM read.actor_arcs"
    )).fetchall()}
    history = {r[0] for r in await (await pg.execute(
        "SELECT actor_id FROM read.actor_arc_history"
    )).fetchall()}
    assert arcs == history == {JUNHO}

    survivors = {r[0] for r in await (await pg.execute(
        "SELECT event_id FROM read.messages"
    )).fetchall()}
    assert survivors == {eid(1, "CD"), sample("player.dm.sent")["event_id"]}


async def test_pg_retire_redelivery_spares_later_redeclaration(pg):
    """ULID 가드 — 순서 뒤집힌 은퇴 재전달은 그 뒤의 새 삶(재선언)을 죽이지 못한다."""
    await pg.execute("DROP SCHEMA IF EXISTS es CASCADE")  # es 불요 경로 — 행 규칙만
    store = ReadStore(pg)
    await store.ensure()
    await store.apply(sample("actor.identity.declared"))
    await store.apply(retire_envelope())

    reborn = dict(sample("actor.identity.declared"), event_id=eid(3, "RB"))
    await store.apply(reborn)
    await store.apply(retire_envelope())  # 재전달 — 과거의 은퇴다

    [(event_id,)] = await (await pg.execute(
        "SELECT event_id FROM read.actors WHERE actor_id = %s", (ARIA,)
    )).fetchall()
    assert event_id == reborn["event_id"]


# ── 리플레이 결정성 — 선 적재 후 은퇴, from-es가 같은 소멸을 재생성한다 ──


async def test_pg_from_es_retirement_replay_is_deterministic(pg):
    await _seed_es(pg)
    comment = sample("player.comment.posted")
    envelopes = [
        ("engine.actor", sample("actor.identity.declared"), None),
        ("engine.actor",
         dict(sample("actor.identity.declared"), event_id=eid(0, "DJ"), actor_id=JUNHO),
         None),
        ("engine.actor", sample("actor.memory.consolidated"), None),
        ("engine.feed",
         dict(sample("feed.post.published"), event_id=eid(1, "PA"), actor_id=ARIA),
         None),
        ("services.gateway",
         dict(comment, event_id=eid(1, "CA"),
              payload=dict(comment["payload"], post_id=eid(1, "PA"))),
         None),
        ("engine.actor",
         dict(sample("actor.message.sent"), event_id=eid(1, "MJ"), actor_id=JUNHO),
         None),
        ("services.gateway", retire_envelope(), None),  # 발행 권한 — permissions.yaml
    ]
    for principal, envelope, key in envelopes:
        await _append_envelope(pg, principal, envelope, stream_key=key)

    store = ReadStore(pg)
    await store.ensure()
    for _ in range(2):  # 리플레이 멱등 — 두 번 돌려도 같은 최종 상태
        fed = await replay_into(pg, PATTERNS["pg"], store.apply)
        assert fed == 6  # feed.post는 pg 술어 밖 — actor 5 + player 1

        actors = {r[0] for r in await (await pg.execute(
            "SELECT actor_id FROM read.actors"
        )).fetchall()}
        assert actors == {JUNHO}  # 아리의 프로필은 소멸, 준호의 삶은 온전
        messages = [r[0] for r in await (await pg.execute(
            "SELECT event_id FROM read.messages"
        )).fetchall()]
        assert messages == [eid(1, "MJ")]  # 아리 포스트의 스레드는 소멸
        episodes = await (await pg.execute(
            "SELECT count(*) FROM read.actor_episodes"
        )).fetchone()
        assert episodes == (0,)
        # verify도 은퇴를 인지한다 — 은퇴 액터 때문에 어긋나지 않는다
        assert (await verify_pg(pg))["ok"]


async def test_kuzu_from_es_retirement_replay_is_deterministic(pg, tmp_path):
    await _seed_es(pg)
    rels = [
        _rel(ARIA, PLAYER), _rel(PLAYER, ARIA), _rel(JUNHO, PLAYER),
    ]
    for i, envelope in enumerate(rels):
        envelope["event_id"] = eid(1, f"R{i}")
        p = envelope["payload"]
        await _append_envelope(
            pg, "engine.relationship", envelope, stream_key=f"{p['from_id']}|{p['to_id']}"
        )
    await _append_envelope(pg, "services.gateway", retire_envelope())

    projector = KuzuProjector(_config(kuzu_dir=str(tmp_path / "kuzu")))
    try:
        for _ in range(2):  # 리플레이 멱등
            fed = await replay_into(pg, PATTERNS["kuzu"], projector.replay_apply())
            assert fed == 4
            assert projector.graph.all_edges(WORLD) == {(JUNHO, PLAYER)}
            assert (await verify_worlds(pg, projector.graph))["ok"]
    finally:
        projector.graph.close()


async def test_redis_from_es_retirement_replay_is_deterministic(pg, redis):
    await _seed_es(pg)
    rel = _rel(ARIA, PLAYER)  # stand-in 팔로우 + 변화 리시트(아리 발신)의 원천
    rel["event_id"] = eid(1, "RA")
    await _append_envelope(
        pg, "engine.relationship", rel, stream_key=f"{ARIA}|{PLAYER}"
    )
    follow = sample("player.follow.changed")  # 준호 아닌 a_minji_kim — 존속 인덱스
    await _append_envelope(pg, "services.gateway", dict(follow, event_id=eid(1, "FW")))
    await _append_envelope(pg, "services.gateway", retire_envelope())

    store = TimelineStore(redis)
    for _ in range(2):  # 리플레이 멱등
        fed = await replay_into(
            pg, PATTERNS["redis"], RedisProjector(_config()).replay_apply(store)
        )
        assert fed == 3
        assert await store.followers(WORLD, ARIA) == set()
        assert await store.followers(
            WORLD, follow["payload"]["target_actor_id"]
        ) == {follow["payload"]["player_id"]}
        # 아리 발신 리시트도 타임라인에서 걷혔다
        assert await redis.zrange(store.timeline_key(WORLD, PLAYER), 0, -1) == []
        assert (await verify_timeline(pg, redis))["ok"]


async def test_verify_detects_unapplied_retirement(pg):
    """원천에는 은퇴가 있는데 프로젝션이 집행하지 않았다 — verify가 드리프트로 잡는다."""
    await _seed_es(pg)
    await _append_envelope(pg, "engine.actor", sample("actor.identity.declared"))
    await _append_envelope(pg, "services.gateway", retire_envelope())

    store = ReadStore(pg)
    await store.ensure()
    await store.apply(sample("actor.identity.declared"))  # 은퇴는 반영하지 않았다

    report = await verify_pg(pg, world_id=WORLD)
    assert not report["ok"]
    assert report["worlds"][WORLD]["tables"]["actors"]["extra"] == [ARIA]


# ── os — 실색인 통합 (LF_TEST_OPENSEARCH_URL 있을 때만, 없으면 skip) ────

OS_URL = os.environ.get("LF_TEST_OPENSEARCH_URL")


@pytest.mark.skipif(OS_URL is None, reason="LF_TEST_OPENSEARCH_URL 미설정 — OpenSearch 통합 스킵")
async def test_os_delete_by_actor_keeps_participant_only_posts():
    index = OpenSearchIndex(OS_URL, "lf-feed-posts-retire-test")
    try:
        await index.drop()
        await index.ensure()
        post = sample("feed.post.published")
        own = envelope_to_doc(dict(post, event_id=eid(1, "PA"), actor_id=ARIA))
        others = envelope_to_doc(dict(
            post, event_id=eid(1, "PB"), actor_id=JUNHO,
            payload=dict(post["payload"], participants=[ARIA]),
        ))
        await index.bulk_upsert([own, others])

        assert await index.delete_by_actor(WORLD, ARIA) == 1
        assert await index.delete_by_actor(WORLD, ARIA) == 0  # 재실행 — 멱등

        async with httpx.AsyncClient(base_url=OS_URL.rstrip("/")) as client:
            r = await client.post(
                "/lf-feed-posts-retire-test/_search", json={"query": {"match_all": {}}}
            )
            r.raise_for_status()
            hits = r.json()["hits"]["hits"]
            # participants에만 낀 남의 포스트는 남는다 — 남의 글은 남의 역사
            assert [h["_id"] for h in hits] == [eid(1, "PB")]
    finally:
        await index.drop()
        await index.close()
