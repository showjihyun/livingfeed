"""조회 범위(from_tick) 계약 — 오늘/이번 주/이번 달의 세계 tick 하한 (ADR-011).

/feed(OS recent·Redis 타임라인)와 프로필 에피소드가 같은 하한 좌표(from_tick,
포함)를 쓴다. OS 경로의 하한은 recent 질의의 range 필터다 (search.py) —
커서는 자연 소진(빈 페이지)으로 끝난다.
"""

import json
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient
from lf_feed_api.config import Config
from lf_feed_api.main import create_app
from lf_feed_api.reads import ProfileReads
from lf_projector.pg_read import ReadStore
from lf_projector.timeline import TimelineStore, ulid_ms

from .conftest import sample

WORLD = "w_main"
ACTOR = "a_aria_kim"
PLAYER = "p_observer_0417"


class FakeSearch:
    """recent 페이지 대역 — 실물처럼 from_tick을 range 필터로 적용한다 (search.py 계약)."""

    def __init__(self, items: list[dict]) -> None:
        self.calls: list[dict] = []
        self.items = items

    async def search(self, world_id, kinds, *, limit, sort, cursor, from_tick=None):
        self.calls.append(
            {
                "world_id": world_id, "kinds": kinds, "limit": limit,
                "sort": sort, "cursor": cursor, "from_tick": from_tick,
            }
        )
        items = [
            i for i in self.items
            if from_tick is None or i.get("tick", -1) >= from_tick
        ]
        next_cursor = items[-1]["event_id"] if items and sort == "recent" else None
        return {"items": items, "next_cursor": next_cursor, "mode": sort}


class FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.timelines: dict[str, list[dict]] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def zrevrange(self, key, start, end):
        docs = sorted(
            self.timelines.get(key, []), key=lambda d: ulid_ms(d["event_id"]), reverse=True
        )
        return [json.dumps(d).encode() for d in docs]


def post(suffix: str, tick: int) -> dict:
    return {"event_id": "01JZK7Q3W000000000000000" + suffix, "tick": tick, "title": "t"}


def make_client(items: list[dict]) -> tuple[TestClient, FakeSearch, FakeCache]:
    search, cache = FakeSearch(items), FakeCache()
    cfg = Config(opensearch_url="http://unused", redis_url="redis://unused")
    app = create_app(cfg=cfg, search=search, cache=cache)
    return TestClient(app), search, cache


def test_from_tick_forces_recent_and_filters_in_query():
    # tick 720이 일 경계 — 719(전날)부터는 범위 밖이다
    client, search, _ = make_client([post("0C", 1081), post("0B", 720), post("0A", 719)])
    body = client.get("/feed", params={"from_tick": 720}).json()
    assert search.calls[-1]["sort"] == "recent"  # 범위 조회는 정의상 시간순
    assert search.calls[-1]["from_tick"] == 720  # 하한은 질의 필터로 내려간다
    assert body["mode"] == "recent"
    assert [i["tick"] for i in body["items"]] == [1081, 720]  # 하한은 포함이다
    # 커서는 자연 소진 — 범위 내 마지막 항목에서 이어보고, 다음 페이지가 비면 끝
    assert body["next_cursor"] == "01JZK7Q3W0000000000000000B"


def test_from_tick_keeps_cursor_when_page_fully_in_range():
    client, _, _ = make_client([post("0C", 1081), post("0B", 720)])
    body = client.get("/feed", params={"from_tick": 700}).json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] == "01JZK7Q3W0000000000000000B"  # 창 안에서 이어보기 계속


def test_from_tick_zero_means_whole_history_and_negative_rejected():
    client, _, _ = make_client([post("0A", 0)])
    assert client.get("/feed", params={"from_tick": 0}).status_code == 200
    assert client.get("/feed", params={"from_tick": -1}).status_code == 422
    assert client.get("/feed", params={"from_tick": "today"}).status_code == 422


def test_from_tick_page_is_not_cached():
    # 하한이 세계일과 함께 움직인다 — 공유 캐시에 굳히면 어제의 "오늘"이 남는다
    client, search, cache = make_client([post("0A", 800)])
    client.get("/feed", params={"from_tick": 720})
    client.get("/feed", params={"from_tick": 720})
    assert len(search.calls) == 2  # 캐시 적중 없음
    assert cache.store == {}


def timeline_entry(suffix: str, tick: int, visibility: str = "private") -> dict:
    return {
        "event_id": "01JZK7Q3W000000000000000" + suffix,
        "tick": tick, "visibility": visibility, "title": "t",
    }


def test_timeline_from_tick_filters_private_entries():
    """Hidden Feed의 범위 조회 — Redis 타임라인 엔트리의 tick으로 서버에서 거른다."""
    client, _, cache = make_client([])
    key = TimelineStore.timeline_key(WORLD, PLAYER)
    cache.timelines[key] = [
        timeline_entry("0C", 1081),
        timeline_entry("0B", 720),
        timeline_entry("0A", 719),
    ]
    body = client.get(
        "/feed", params={"types": "private", "player_id": PLAYER, "from_tick": 720}
    ).json()
    assert [i["tick"] for i in body["items"]] == [1081, 720]  # 하한 포함, 719 제외

    whole = client.get("/feed", params={"types": "private", "player_id": PLAYER}).json()
    assert len(whole["items"]) == 3  # from_tick 없으면 현행 그대로


def test_profile_passes_episode_from_tick_through():
    class FakeReads:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def actor_profile(
            self, world_id, actor_id, *, episode_limit, episode_cursor, episode_from_tick=None
        ):
            self.calls.append((world_id, actor_id, episode_limit, episode_cursor,
                               episode_from_tick))
            return {"world_id": world_id, "actor_id": actor_id, "identity": None,
                    "beliefs": [], "episodes": {"items": [], "next_cursor": None},
                    "arc": None, "arc_history": []}

    reads = FakeReads()
    cfg = Config(opensearch_url="http://unused", redis_url="redis://unused")
    app = create_app(cfg=cfg, search=object(), cache=FakeCache(), reads=reads)
    client = TestClient(app)
    assert client.get(
        "/actors/a_aria_kim/profile", params={"episode_from_tick": 720}
    ).status_code == 200
    assert reads.calls[-1] == (WORLD, ACTOR, 20, None, 720)
    assert client.get(
        "/actors/a_aria_kim/profile", params={"episode_from_tick": -1}
    ).status_code == 422


# ── PG 통합 — read 테이블의 tick 하한 (LF_TEST_* 게이트, conftest.pg) ──────────


class OneConnPool:
    """test_read_integration과 같은 대역 — 단일 연결을 pool.connection() 모양으로."""

    def __init__(self, conn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def connection(self):
        yield self._conn


async def test_episode_from_tick_windows_pages(pg):
    """에피소드의 범위 조회 — tick 하한이 커서 페이지네이션과 함께 걸린다."""
    store = ReadStore(pg)
    await store.ensure()
    base = sample("actor.memory.consolidated")
    for suffix, tick in (("A", 350), ("B", 720), ("C", 1081)):
        await store.apply(dict(base, event_id=base["event_id"][:-1] + suffix, tick=tick))

    reads = ProfileReads(OneConnPool(pg))
    windowed = await reads.actor_profile(
        WORLD, ACTOR, episode_limit=10, episode_cursor=None, episode_from_tick=720
    )
    assert [e["tick"] for e in windowed["episodes"]["items"]] == [1081, 720]  # 350 제외

    # 같은 창 안에서 더 과거로 — 커서가 하한을 넘어 새지 않는다
    first = await reads.actor_profile(
        WORLD, ACTOR, episode_limit=1, episode_cursor=None, episode_from_tick=720
    )
    rest = await reads.actor_profile(
        WORLD, ACTOR, episode_limit=10,
        episode_cursor=first["episodes"]["next_cursor"], episode_from_tick=720,
    )
    assert [e["tick"] for e in rest["episodes"]["items"]] == [720]

    # 하한 없음 — 현행 그대로 전체 역사
    whole = await reads.actor_profile(
        WORLD, ACTOR, episode_limit=10, episode_cursor=None
    )
    assert len(whole["episodes"]["items"]) == 3


async def test_threads_carry_last_tick(pg):
    """인박스 스레드가 마지막 메시지의 세계 tick을 동봉한다 — FE '받은 것' 범위 좌표."""
    store = ReadStore(pg)
    await store.ensure()
    await store.apply(sample("player.dm.sent"))       # …G: tick 43
    await store.apply(sample("actor.message.sent"))   # …K: tick 44 — 스레드의 마지막
    listing = await ProfileReads(OneConnPool(pg)).threads(WORLD, PLAYER, limit=10)
    [thread] = listing["threads"]
    assert thread["actor_id"] == ACTOR
    assert thread["last_tick"] == 44
