"""PG read 왕복 통합 — pg-projector가 쓰고 ProfileReads가 읽는다 (PG 게이트).

쓰기(프로젝터)와 읽기(API)가 같은 테이블 계약을 공유하는지가 검증의 핵심이다.
"""

from contextlib import asynccontextmanager

from lf_feed_api.reads import ProfileReads
from lf_projector.pg_read import ReadStore

from .conftest import sample

WORLD = "w_main"
ACTOR = "a_aria_kim"
PLAYER = "p_observer_0417"


class OneConnPool:
    """테스트 대역 — 단일 연결을 psycopg_pool.connection() 모양으로 감싼다."""

    def __init__(self, conn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def connection(self):
        yield self._conn


async def seed(pg) -> None:
    store = ReadStore(pg)
    await store.ensure()
    for name in (
        "actor.identity.declared",
        "actor.memory.consolidated", "actor.belief.formed",
        "player.dm.sent", "actor.message.sent",
        "system.director.arc_planned",  # 대상은 a_minji_kim — 아리아에겐 아크가 없다
    ):
        await store.apply(sample(name))


async def test_actor_profile_roundtrip(pg):
    await seed(pg)
    profile = await ProfileReads(OneConnPool(pg)).actor_profile(
        WORLD, ACTOR, episode_limit=10, episode_cursor=None
    )
    # 정체성(read.actors)이 프로필에 실린다 — FE는 이 이름을 쓴다 (하드코딩 금지)
    assert profile["identity"]["name"] == "김아리"
    assert profile["identity"]["archetype"] == "ambitious_journalist"
    [belief] = profile["beliefs"]
    assert belief["kind"] == "supporter"
    assert belief["about_id"] == PLAYER  # '-' 정규화가 아닌 실제 대상은 그대로
    assert belief["revisions"] == 1
    [episode] = profile["episodes"]["items"]
    assert "지지해줬다" in episode["summary"]
    assert episode["factors"]["emotion"] == 0.83  # jsonb → dict
    assert profile["episodes"]["next_cursor"] == episode["event_id"]
    assert profile["arc"] is None  # 아크 없는 액터 — 그저 일상 (FE 폴백 규약)


async def test_actor_profile_includes_arc(pg):
    """인생 아크(read.actor_arcs)가 프로필에 실린다 — "인생의 장" (ADR-013/plan-08)."""
    await seed(pg)
    profile = await ProfileReads(OneConnPool(pg)).actor_profile(
        WORLD, "a_minji_kim", episode_limit=10, episode_cursor=None
    )
    assert profile["arc"]["stage"] == "settling"
    assert "이직" in profile["arc"]["intention"]
    assert profile["arc"]["planned_at"] is not None
    # 연대기 — 장의 흐름 (지금은 첫 장 하나)
    [chapter] = profile["arc_history"]
    assert chapter["stage"] == "settling"


async def test_actors_list_reads_identity(pg):
    await seed(pg)
    listing = await ProfileReads(OneConnPool(pg)).actors(WORLD)
    assert [a["name"] for a in listing] == ["김아리"]
    assert listing[0]["actor_id"] == ACTOR


async def test_episode_cursor_pages_backwards(pg):
    store = ReadStore(pg)
    await store.ensure()
    base = sample("actor.memory.consolidated")
    for suffix in ("A", "B", "C"):
        await store.apply(dict(base, event_id=base["event_id"][:-1] + suffix))

    reads = ProfileReads(OneConnPool(pg))
    first = await reads.actor_profile(WORLD, ACTOR, episode_limit=2, episode_cursor=None)
    ids = [e["event_id"][-1] for e in first["episodes"]["items"]]
    assert ids == ["C", "B"]  # 최신부터

    rest = await reads.actor_profile(
        WORLD, ACTOR, episode_limit=2, episode_cursor=first["episodes"]["next_cursor"]
    )
    assert [e["event_id"][-1] for e in rest["episodes"]["items"]] == ["A"]


async def test_conversation_merges_both_directions(pg):
    await seed(pg)
    convo = await ProfileReads(OneConnPool(pg)).conversation(
        WORLD, PLAYER, ACTOR, limit=10, cursor=None
    )
    # 시간 역순: 액터 답장(…K)이 플레이어 DM(…G)보다 나중이다
    assert [m["sender"] for m in convo["items"]] == ["actor", "player"]
    assert convo["items"][0]["channel"] == "dm"
    assert convo["next_cursor"] == convo["items"][-1]["event_id"]

    # 다른 플레이어의 대화창은 비어 있다 — 대화는 (player, actor) 쌍 단위다
    other = await ProfileReads(OneConnPool(pg)).conversation(
        WORLD, "p_stranger", ACTOR, limit=10, cursor=None
    )
    assert other["items"] == []


async def test_post_comments_thread_roundtrip(pg):
    """댓글 스레드 왕복 — 플레이어·액터 댓글이 post_id로 모이고 이름이 실린다.

    도파민 루프의 되읽기 계약: 시간순, author_kind 구분, 액터 저자는
    read.actors의 표시 이름 동봉 (이름 그라운딩 — FE가 원시 id를 싣지 않는다).
    """
    store = ReadStore(pg)
    await store.ensure()
    await store.apply(sample("actor.identity.declared"))  # 김아리 — 이름의 원천
    player_comment = sample("player.comment.posted")      # …H: 최상위, post F
    post_id = player_comment["payload"]["post_id"]
    reply = sample("actor.message.sent")
    base_id = reply["event_id"][:-1]
    # …M: 아리의 답글 — 플레이어 댓글에 대한 depth-1 답장
    actor_reply = dict(
        reply, event_id=base_id + "M",
        payload=dict(reply["payload"], channel="comment", post_id=post_id,
                     in_reply_to=player_comment["event_id"]),
    )
    # …N: 이름 미선언 액터의 소셜 댓글 — author_name은 null로 남는다 (FE '누군가' 폴백)
    social = dict(
        reply, event_id=base_id + "N", actor_id="a_junho_park",
        payload={"channel": "comment", "target_player_id": None,
                 "target_actor_id": ACTOR, "text": "소셜 루프",
                 "post_id": post_id, "in_reply_to": post_id},
    )
    for envelope in (player_comment, actor_reply, social):
        await store.apply(envelope)

    reads = ProfileReads(OneConnPool(pg))
    thread = await reads.post_comments(WORLD, post_id, limit=10)
    assert thread["post_id"] == post_id
    items = thread["items"]
    # 시간순(event_id ULID 오름차순) — 대화는 처음부터 읽는다
    assert [i["author_kind"] for i in items] == ["player", "actor", "actor"]
    assert [i["event_id"] for i in items] == sorted(i["event_id"] for i in items)

    mine, aria, junho = items
    assert mine["author_id"] == PLAYER
    assert mine["author_name"] is None      # 플레이어 표시명은 FE 몫 ('나')
    assert mine["in_reply_to"] is None      # 플레이어 댓글은 항상 최상위
    assert "멋져요" in mine["text"]
    assert aria["author_id"] == ACTOR
    assert aria["author_name"] == "김아리"  # read.actors 조인 — 이름 그라운딩
    assert aria["in_reply_to"] == mine["event_id"]  # 답장 판별 근거
    assert junho["author_name"] is None     # 미선언 액터 — FE가 '누군가'로 그린다
    assert junho["in_reply_to"] == post_id  # 최상위 소셜 댓글

    # limit은 스레드 앞에서부터 자른다
    first = await reads.post_comments(WORLD, post_id, limit=1)
    assert [i["event_id"] for i in first["items"]] == [mine["event_id"]]

    # 다른 포스트의 스레드는 비어 있다 — DM(post_id null)도 섞이지 않는다
    await store.apply(sample("player.dm.sent"))
    empty = await reads.post_comments(WORLD, "0" * 26, limit=10)
    assert empty["items"] == []


async def test_threads_last_message_per_actor_newest_first(pg):
    """인박스 집계 — 액터별 마지막 1건, 최신 대화 순 (선제 DM도 그저 첫 마디다)."""
    store = ReadStore(pg)
    await store.ensure()
    player_dm = sample("player.dm.sent")        # …G: 플레이어 → 아리
    reply = sample("actor.message.sent")        # …K: 아리 → 플레이어 (dm)
    base_id = reply["event_id"][:-1]
    # …R: 준호의 선제 DM — in_reply_to 없음, 새 스레드의 첫 마디
    proactive = dict(
        reply, event_id=base_id + "R", actor_id="a_junho_park", causation_id=None,
        payload=dict(reply["payload"], text="문득 생각나서요.", in_reply_to=None),
    )
    # …S: 액터↔액터 댓글 — counterpart가 a_*라 플레이어 인박스를 오염시키지 않는다
    social = dict(
        reply, event_id=base_id + "S", actor_id="a_junho_park",
        payload={"channel": "comment", "target_player_id": None,
                 "target_actor_id": ACTOR, "text": "소셜 루프",
                 "post_id": base_id + "0", "in_reply_to": None},
    )
    for envelope in (player_dm, reply, proactive, social):
        await store.apply(envelope)

    reads = ProfileReads(OneConnPool(pg))
    listing = await reads.threads(WORLD, PLAYER, limit=10)
    # 최신 대화(준호 …R)가 먼저 — 액터↔액터(…S)는 준호 스레드를 덮지 않는다
    assert [t["actor_id"] for t in listing["threads"]] == ["a_junho_park", ACTOR]
    junho, aria = listing["threads"]
    assert junho["last_event_id"] == base_id + "R"
    assert junho["last_from_actor"] is True
    assert junho["last_text"] == "문득 생각나서요."
    # 아리 스레드의 마지막은 답장(…K) — 플레이어 발신(…G)이 아니다
    assert aria["last_event_id"] == reply["event_id"]
    assert aria["last_channel"] == "dm"
    assert aria["last_at"] is not None

    # limit은 스레드 수를 자른다 — 가장 최근 대화부터
    top = await reads.threads(WORLD, PLAYER, limit=1)
    assert [t["actor_id"] for t in top["threads"]] == ["a_junho_park"]

    # 다른 플레이어의 인박스는 비어 있다
    stranger = await reads.threads(WORLD, "p_stranger", limit=10)
    assert stranger["threads"] == []
