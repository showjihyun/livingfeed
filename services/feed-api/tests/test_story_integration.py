"""es 사슬 왕복 통합 — append(SoT 쓰기)와 StoryReads(사슬 읽기)의 계약 공유 (PG 게이트).

정렬(global_seq)·무서사 제외·상한·이름 해석·'당신' 치환·started_by_you를
실제 PG 왕복으로 검증한다. es 직접 읽기의 예외 근거는 story.py 머리말.
"""

from contextlib import asynccontextmanager

from lf_eventstore import NewEvent, append
from lf_eventstore.migrate import migrate
from lf_feed_api.story import StoryReads
from lf_projector.pg_read import ReadStore

from .conftest import sample

WORLD = "w_main"
PLAYER = "p_observer_0417"
#: 사슬 뿌리 — 플레이어 DM 이벤트 id (correlation_id, 샘플과 동일)
CHAIN = "01JZK7Q3W0000000000000000G"


class OneConnPool:
    """테스트 대역 — 단일 연결을 psycopg_pool.connection() 모양으로 감싼다."""

    def __init__(self, conn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def connection(self):
        yield self._conn


async def _append_env(pg, principal: str, env: dict, *, head: int) -> None:
    """봉투 하나를 event_id·correlation_id 그대로 es에 적재한다."""
    key = env.get("actor_id") or env["payload"].get("player_id") or "k"
    await append(
        pg, principal,
        [NewEvent(
            world_id=env["world_id"], stream=env["stream"], stream_key=key,
            type=env["type"], tick=env["tick"], actor_id=env.get("actor_id"),
            payload=env["payload"], event_id=env["event_id"],
            causation_id=env.get("causation_id"), correlation_id=env["correlation_id"],
        )],
        expected_head=head,
    )


async def _append_sample(pg, principal: str, name: str, *, head: int) -> None:
    """샘플 봉투를 원래 모습 그대로 es에 적재한다."""
    await _append_env(pg, principal, sample(name), head=head)


async def seed_chain(pg) -> None:
    """샘플의 correlation G 사슬: 플레이어 DM → (tick 소음) → 감정 → 답장 → 관계.

    global_seq(적재 순서)가 곧 타임라인 순서다 — 샘플의 correlation_id를 그대로 쓴다.
    system.tick.started 샘플도 correlation G라 무서사 제외의 실측 재료가 된다.
    """
    await pg.execute("DROP SCHEMA IF EXISTS es CASCADE")
    await migrate(pg)
    await _append_sample(pg, "engine.actor", "actor.identity.declared", head=0)  # 다른 사슬(W)
    await _append_sample(pg, "services.gateway", "player.dm.sent", head=0)       # 사슬 시작(G)
    await _append_sample(pg, "engine.tick", "system.tick.started", head=0)       # 소음 — 제외돼야
    await _append_sample(pg, "engine.actor", "actor.emotion.shifted", head=1)
    await _append_sample(pg, "engine.actor", "actor.message.sent", head=2)
    await _append_sample(pg, "engine.relationship", "relationship.state.changed", head=0)


async def seed_names(pg) -> None:
    """read.actors — 이름 해석의 원천 (pg fixture가 read 스키마를 비워둔 상태)."""
    store = ReadStore(pg)
    await store.ensure()
    await store.apply(sample("actor.identity.declared"))


async def test_chain_orders_excludes_noise_and_marks_authorship(pg):
    await seed_chain(pg)
    await seed_names(pg)
    body = await StoryReads(OneConnPool(pg)).timeline(
        WORLD, CHAIN, player_id=PLAYER, limit=50
    )
    # global_seq 순 + system.tick.* 제외 (무서사)
    assert [i["type"] for i in body["items"]] == [
        "player.dm.sent", "actor.emotion.shifted",
        "actor.message.sent", "relationship.state.changed",
    ]
    # 이름 해석: 요청자는 '당신', 액터는 read.actors의 이름
    assert [i["actor"] for i in body["items"]] == ["당신", "김아리", "김아리", "김아리"]
    # 요약: payload의 사람 문장 (dm text → emotion reason → …)
    assert body["items"][0]["summary"] == sample("player.dm.sent")["payload"]["text"]
    assert "인정받았다" in body["items"][1]["summary"]
    # 저자성 — 이 이야기의 원작자가 나 (plan/03 §단계 3→4)
    assert body["origin"] == body["items"][0]
    assert body["started_by_you"] is True


async def test_other_requesters_do_not_own_the_story(pg):
    await seed_chain(pg)
    stranger = await StoryReads(OneConnPool(pg)).timeline(
        WORLD, CHAIN, player_id="p_stranger", limit=50
    )
    # 남의 개입은 익명 — 관찰자 프라이버시
    assert stranger["items"][0]["actor"] == "어느 관찰자"
    assert stranger["started_by_you"] is False

    anonymous = await StoryReads(OneConnPool(pg)).timeline(
        WORLD, CHAIN, player_id=None, limit=50
    )
    assert anonymous["started_by_you"] is False


async def test_actor_origin_is_not_yours_and_no_type_label_leaks(pg):
    await seed_chain(pg)
    # 사슬 W: 액터의 정체성 선언 하나 — origin이 player.*가 아니다
    body = await StoryReads(OneConnPool(pg)).timeline(
        WORLD, sample("actor.identity.declared")["correlation_id"],
        player_id=PLAYER, limit=50,
    )
    assert [i["type"] for i in body["items"]] == ["actor.identity.declared"]
    assert body["started_by_you"] is False
    # 정체성 선언의 사람 문장은 bio다 — 타입 라벨(기계 어휘)은 문장에 싣지 않는다
    assert body["items"][0]["summary"] == sample("actor.identity.declared")["payload"]["bio"]
    assert "actor.identity.declared" not in body["items"][0]["summary"]


async def test_limit_caps_narrative_events(pg):
    await seed_chain(pg)
    body = await StoryReads(OneConnPool(pg)).timeline(WORLD, CHAIN, player_id=PLAYER, limit=2)
    # 상한은 '이야기 항목' 기준이다 — 제외 타입(tick)이 자리를 차지하지 않는다
    assert [i["type"] for i in body["items"]] == ["player.dm.sent", "actor.emotion.shifted"]


async def test_missing_read_schema_degrades_to_anonymous_names(pg):
    await seed_chain(pg)  # read.actors 미시딩 — pg fixture가 read 스키마를 지운 상태
    body = await StoryReads(OneConnPool(pg)).timeline(WORLD, CHAIN, player_id=PLAYER, limit=50)
    # 이름 해석은 장식 — read 미구축이 사슬 조회를 죽이지 않는다 (익명 폴백)
    assert body["items"][1]["actor"] == "누군가"
    assert body["started_by_you"] is True


async def test_unknown_chain_is_empty_not_error(pg):
    await seed_chain(pg)
    body = await StoryReads(OneConnPool(pg)).timeline(
        WORLD, "01JZK7Q3W000000000000000ZZ"[:26], player_id=PLAYER, limit=50
    )
    assert body["items"] == []
    assert body["origin"] is None
    assert body["started_by_you"] is False


# ── 실사고 재현 — 표시측 정화 (2026-07 실화면 유출 + 과거 발행분 원시 id) ──


async def seed_leaky_chain(pg) -> str:
    """유출 상태의 사슬을 es에 그대로 적재한다 — es는 불변이라 고칠 수 없는 것들:
    emotion 엔진의 기계 사유, composer 정화 이전 발행분의 원시 id 제목·본문."""
    await pg.execute("DROP SCHEMA IF EXISTS es CASCADE")
    await migrate(pg)
    comment = sample("player.comment.posted")
    chain = comment["event_id"]
    await _append_env(pg, "services.gateway", dict(comment, correlation_id=chain), head=0)
    emotion = sample("actor.emotion.shifted")
    await _append_env(pg, "engine.actor", dict(
        emotion, correlation_id=chain,
        payload=dict(
            emotion["payload"],
            reason="player.comment.posted — gratitude 0.75 (플레이어 p_observer_0417)",
        ),
    ), head=0)
    post = sample("feed.post.published")
    await _append_env(pg, "engine.feed", dict(
        post, correlation_id=chain,
        payload=dict(
            post["payload"],
            title="a_aria_kim, p_observer_0417에게 답하다",
            body="p_observer_0417에게 전하고 싶은 말이 있었다",
        ),
    ), head=0)
    return chain


async def test_machine_reason_and_stale_ids_are_sanitized_in_chain(pg):
    chain = await seed_leaky_chain(pg)
    await seed_names(pg)
    body = await StoryReads(OneConnPool(pg)).timeline(
        WORLD, chain, player_id=PLAYER, limit=50
    )
    # 뼈대 생존 + 구조 필드(type)는 정화 대상이 아니다
    assert [i["type"] for i in body["items"]] == [
        "player.comment.posted", "actor.emotion.shifted", "feed.post.published",
    ]
    comment_item, emotion_item, post_item = body["items"]
    # 시작점(뼈대)의 사람 문장은 원문 그대로
    assert comment_item["summary"] == sample("player.comment.posted")["payload"]["text"]
    # 실화면 유출 원문 → 사람 문장 (타입 토큰·감정 코드·수치·플레이어 id 소거)
    assert emotion_item["summary"] == "댓글에 마음이 움직였다"
    # 과거 발행분 제목 — 액터 id는 read.actors 실명, 플레이어 id는 세계 어휘
    assert post_item["summary"] == "김아리, 어느 관찰자에게 답하다"
    joined = " ".join(i["summary"] for i in body["items"])
    for token in ("gratitude", "0.75", "p_observer_0417", "a_aria_kim",
                  "player.comment.posted"):
        assert token not in joined
    assert body["started_by_you"] is True


async def test_derived_post_title_is_sanitized_at_read_boundary(pg):
    chain = await seed_leaky_chain(pg)
    await seed_names(pg)
    summary = await StoryReads(OneConnPool(pg)).derived_posts(WORLD, chain)
    assert summary["count"] == 1
    assert summary["latest"]["title"] == "김아리, 어느 관찰자에게 답하다"
    assert summary["latest"]["author"] == "김아리"


# ── 파생 포스트 요약 — "이 대화가 낳은 이야기" (댓글 스레드 배지 재료) ────


async def seed_derived_chain(pg) -> str:
    """원 포스트 F에 대한 개입 사슬(correlation = F, gateway 규약)과 그 사슬을
    승계한 후속 포스트 2건을 적재한다. 판정은 loop_health 드라마 재생산과 동일.

    반환: 사슬 좌표(원 포스트 event_id).
    """
    await pg.execute("DROP SCHEMA IF EXISTS es CASCADE")
    await migrate(pg)
    post = sample("feed.post.published")
    post_id = post["event_id"]  # …F
    # 원 포스트 — 자기 뿌리 사슬(correlation = 자기 자신)로 적재해 자기 제외를 실측
    await _append_env(pg, "engine.feed", dict(post, correlation_id=post_id), head=0)
    # 플레이어 댓글 — 개입 correlation = post_id (gateway 규약)
    comment = sample("player.comment.posted")
    await _append_env(
        pg, "services.gateway", dict(comment, correlation_id=post_id), head=0
    )
    base = post_id[:-1]
    # 사슬을 승계한 2차 사건 — 아리(선언된 이름)의 후속 포스트
    await _append_env(pg, "engine.feed", dict(
        post, event_id=base + "X", correlation_id=post_id,
        payload=dict(post["payload"], title="김아리, 그 말을 오래 생각하다"),
    ), head=1)
    # 가장 최근의 2차 사건 — 이름 미선언 액터 (표시 이름 '누군가' 폴백 실측)
    await _append_env(pg, "engine.feed", dict(
        post, event_id=base + "Y", actor_id="a_junho_park", correlation_id=post_id,
        payload=dict(post["payload"], title="준호, 답을 내놓다"),
    ), head=0)
    return post_id


async def test_derived_posts_counts_and_surfaces_latest(pg):
    post_id = await seed_derived_chain(pg)
    await seed_names(pg)  # 김아리만 선언 — 준호는 미선언
    summary = await StoryReads(OneConnPool(pg)).derived_posts(WORLD, post_id)
    # 원 포스트 자신은 세지 않는다 — 사슬이 '낳은' 포스트만 (댓글도 포스트가 아니다)
    assert summary["count"] == 2
    latest = summary["latest"]
    assert latest["post_id"] == post_id[:-1] + "Y"  # 적재 순 마지막
    assert latest["title"] == "준호, 답을 내놓다"
    # 이름 그라운딩 — 미선언 액터는 '누군가' 폴백 (원시 id를 문장에 싣지 않는다)
    assert latest["author"] == "누군가"
    assert latest["occurred_at"] is not None


async def test_derived_posts_grounds_declared_actor_name(pg):
    post_id = await seed_derived_chain(pg)
    await seed_names(pg)
    pool = OneConnPool(pg)
    # 최신 1건이 선언된 액터라면 read.actors의 이름이 실린다
    await pg.execute(
        "DELETE FROM es.events WHERE event_id = %s", (post_id[:-1] + "Y",)
    )
    summary = await StoryReads(pool).derived_posts(WORLD, post_id)
    assert summary["count"] == 1
    assert summary["latest"]["author"] == "김아리"
    assert summary["latest"]["title"] == "김아리, 그 말을 오래 생각하다"


async def test_derived_posts_empty_chain_is_zero_not_error(pg):
    await seed_derived_chain(pg)
    summary = await StoryReads(OneConnPool(pg)).derived_posts(
        WORLD, "01JZK7Q3W000000000000000ZZ"[:26]
    )
    assert summary == {"count": 0, "latest": None}


async def test_derived_posts_join_existing_story_surface(pg):
    """합류 검증 — 배지가 가리키는 곳은 기존 /story 사슬이다 (신규 화면 없음).

    같은 correlation으로 timeline을 조회하면 개입(플레이어 댓글)이 시작점이고
    2차 사건(후속 포스트)이 같은 사슬 위에 보인다.
    """
    post_id = await seed_derived_chain(pg)
    await seed_names(pg)
    body = await StoryReads(OneConnPool(pg)).timeline(
        WORLD, post_id, player_id=PLAYER, limit=50
    )
    types = [i["type"] for i in body["items"]]
    assert types == [
        "feed.post.published",  # 원 포스트(자기 뿌리 사슬)
        "player.comment.posted", "feed.post.published", "feed.post.published",
    ]
    # 후속 포스트의 요약은 제목 — 사람 문장이 사슬에 실린다
    assert body["items"][-1]["summary"] == "준호, 답을 내놓다"
