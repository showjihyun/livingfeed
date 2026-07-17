"""읽기 모델 엔드포인트 계약 — 프로필/대화(PG read), personal 타임라인(Redis).

계약 검증은 대역 주입(기존 /feed 테스트 방식), 실제 저장소 왕복은
LF_TEST_* 게이트가 있는 통합 테스트(test_read_integration.py)가 맡는다.
"""

import json

from fastapi.testclient import TestClient
from lf_feed_api.config import Config
from lf_feed_api.main import create_app
from lf_projector.timeline import TimelineStore, ulid_ms

WORLD = "w_main"
PLAYER = "p_observer_0417"
CURSOR = "01JZK7Q3W0000000000000000A"


class FakeReads:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def actors(self, world_id):
        self.calls.append(("actors", world_id))
        return [{"actor_id": "a_x", "name": "이름", "archetype": "arch", "bio": "b", "goals": []}]

    async def actor_profile(self, world_id, actor_id, *, episode_limit, episode_cursor):
        self.calls.append(("profile", world_id, actor_id, episode_limit, episode_cursor))
        return {"world_id": world_id, "actor_id": actor_id, "identity": None, "beliefs": [],
                "episodes": {"items": [], "next_cursor": None}, "arc": None,
                "arc_history": []}

    async def conversation(self, world_id, player_id, actor_id, *, limit, cursor):
        self.calls.append(("conversation", world_id, player_id, actor_id, limit, cursor))
        return {"items": [], "next_cursor": None, "mode": "recent"}

    async def threads(self, world_id, player_id, *, limit):
        self.calls.append(("threads", world_id, player_id, limit))
        return {"threads": []}


class FakeTimelineRedis:
    """캐시 겸 타임라인 대역 — /feed가 쓰는 get/setex와 zrevrange만 구현한다."""

    def __init__(self) -> None:
        self.timelines: dict[str, list[dict]] = {}

    async def get(self, key):
        return None

    async def setex(self, key, ttl, value):
        pass

    async def zrevrange(self, key, start, end):
        docs = sorted(
            self.timelines.get(key, []), key=lambda d: ulid_ms(d["event_id"]), reverse=True
        )
        return [json.dumps(d).encode() for d in docs]


def entry(event_id: str, visibility: str) -> dict:
    return {"event_id": event_id, "visibility": visibility, "title": "t"}


def make_client() -> tuple[TestClient, FakeReads, FakeTimelineRedis]:
    reads, cache = FakeReads(), FakeTimelineRedis()
    cfg = Config(opensearch_url="http://unused", redis_url="redis://unused")
    app = create_app(cfg=cfg, search=object(), cache=cache, reads=reads)  # search는 안 닿는다
    return TestClient(app), reads, cache


def test_timeline_kinds_cannot_mix_with_os_kinds():
    client, _, _ = make_client()
    resp = client.get("/feed", params={"types": "personal,world", "player_id": PLAYER})
    assert resp.status_code == 400
    assert "섞어" in resp.json()["detail"]


def test_timeline_requires_player_id():
    client, _, _ = make_client()
    assert client.get("/feed", params={"types": "personal"}).status_code == 400


def test_personal_excludes_private_and_pages_by_cursor():
    client, _, cache = make_client()
    key = TimelineStore.timeline_key(WORLD, PLAYER)
    cache.timelines[key] = [
        entry("01JZK7Q3W0000000000000000C", "world"),    # 아는 액터의 공개 포스트
        entry("01JZK7Q3W0000000000000000B", "private"),  # 1:1 답장
        entry("01JZK7Q3W0000000000000000A", "world"),
    ]
    body = client.get(
        "/feed", params={"types": "personal", "player_id": PLAYER}
    ).json()
    assert [i["event_id"][-1] for i in body["items"]] == ["C", "A"]  # private 제외
    assert body["mode"] == "recent"

    paged = client.get(
        "/feed",
        params={"types": "personal,private", "player_id": PLAYER,
                "cursor": "01JZK7Q3W0000000000000000C"},
    ).json()
    assert [i["event_id"][-1] for i in paged["items"]] == ["B", "A"]  # 커서 이후 + private 포함
    assert paged["next_cursor"] == "01JZK7Q3W0000000000000000A"


def test_actors_list_delegates():
    client, reads, _ = make_client()
    resp = client.get("/actors", params={"world_id": "w_main"})
    assert resp.status_code == 200
    assert resp.json()["actors"][0]["name"] == "이름"
    assert reads.calls[-1] == ("actors", "w_main")


def test_profile_and_messages_delegate_with_clamped_limits():
    client, reads, _ = make_client()
    assert client.get(
        "/actors/a_aria_kim/profile", params={"episode_limit": 500}
    ).status_code == 200
    assert reads.calls[-1] == ("profile", "w_main", "a_aria_kim", 100, None)

    assert client.get(
        "/messages", params={"player_id": PLAYER, "actor_id": "a_aria_kim"}
    ).status_code == 200
    assert reads.calls[-1] == ("conversation", "w_main", PLAYER, "a_aria_kim", 50, None)


def test_threads_delegates_with_clamped_limit():
    client, reads, _ = make_client()
    resp = client.get("/messages/threads", params={"player_id": PLAYER, "limit": 500})
    assert resp.status_code == 200
    assert resp.json() == {"threads": []}
    assert reads.calls[-1] == ("threads", "w_main", PLAYER, 100)


def test_threads_rejects_bad_player_id():
    client, _, _ = make_client()
    assert client.get("/messages/threads", params={"player_id": "a_x"}).status_code == 422
    assert client.get("/messages/threads").status_code == 422  # player_id는 필수


def test_bad_ids_and_cursors_rejected():
    client, _, _ = make_client()
    assert client.get("/actors/notanactor/profile").status_code == 422  # 경로 패턴(a_ 접두)
    assert client.get(
        "/messages", params={"player_id": "nope", "actor_id": "a_aria_kim"}
    ).status_code == 422
    assert client.get(
        "/actors/a_aria_kim/profile", params={"episode_cursor": "not-ulid"}
    ).status_code == 400


def test_reads_unavailable_returns_503_without_killing_feed():
    cfg = Config(opensearch_url="http://unused", redis_url="redis://unused")
    cache = FakeTimelineRedis()
    app = create_app(cfg=cfg, search=object(), cache=cache, reads=None)
    app.state.reads = None  # lifespan이 PG 실패로 내려놓은 상태를 재현
    client = TestClient(app)
    assert client.get("/actors/a_aria_kim/profile").status_code == 503
    assert client.get(
        "/messages/threads", params={"player_id": PLAYER}
    ).status_code == 503
    # 타임라인 경로는 PG와 무관하게 살아 있다 (장애 격리, ADR-003 계약 5)
    assert client.get(
        "/feed", params={"types": "personal", "player_id": PLAYER}
    ).status_code == 200
