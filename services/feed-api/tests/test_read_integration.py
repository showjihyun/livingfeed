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
