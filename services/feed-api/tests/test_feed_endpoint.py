"""GET /feed 계약 검증 — 파라미터 파싱, 커서 규칙, 캐시 (ADR-014 §2단)."""

from fastapi.testclient import TestClient
from lf_feed_api.config import Config
from lf_feed_api.main import create_app

CURSOR = "01JZK7Q3W0000000000000000A"


class FakeSearch:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.text_calls: list[dict] = []
        self.items = [{"event_id": CURSOR, "title": "t"}]

    async def search(self, world_id, kinds, *, limit, sort, cursor, from_tick=None):
        self.calls.append(
            {"world_id": world_id, "kinds": kinds, "limit": limit, "sort": sort, "cursor": cursor}
        )
        next_cursor = self.items[-1]["event_id"] if sort == "recent" else None
        return {"items": self.items, "next_cursor": next_cursor, "mode": sort}

    async def text_search(self, world_id, kinds, *, q, actor_ids, limit):
        self.text_calls.append(
            {"world_id": world_id, "kinds": kinds, "q": q, "actor_ids": actor_ids, "limit": limit}
        )
        return {"items": self.items, "query": q}


class FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def aclose(self):
        pass


def make_client() -> tuple[TestClient, FakeSearch, FakeCache]:
    search, cache = FakeSearch(), FakeCache()
    cfg = Config(opensearch_url="http://unused", redis_url="redis://unused")
    app = create_app(cfg=cfg, search=search, cache=cache)
    return TestClient(app), search, cache


def test_default_is_ranked_world_first_page():
    client, search, _ = make_client()
    resp = client.get("/feed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "ranked"
    assert body["next_cursor"] is None
    assert search.calls == [
        {"world_id": "w_main", "kinds": ["world"], "limit": 20, "sort": "ranked", "cursor": None}
    ]


def test_types_csv_is_parsed_and_validated():
    client, search, _ = make_client()
    assert client.get("/feed", params={"types": "community, world"}).status_code == 200
    assert search.calls[-1]["kinds"] == ["community", "world"]

    resp = client.get("/feed", params={"types": "world,doom"})
    assert resp.status_code == 400
    assert "doom" in resp.json()["detail"]


def test_cursor_forces_recent_and_returns_next_cursor():
    client, search, _ = make_client()
    resp = client.get("/feed", params={"cursor": CURSOR})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "recent"
    assert resp.json()["next_cursor"] == CURSOR
    assert search.calls[-1]["sort"] == "recent"
    assert search.calls[-1]["cursor"] == CURSOR


def test_invalid_cursor_rejected():
    client, _, _ = make_client()
    assert client.get("/feed", params={"cursor": "not-a-ulid"}).status_code == 400


def test_invalid_sort_rejected():
    client, _, _ = make_client()
    assert client.get("/feed", params={"sort": "viral"}).status_code == 400


def test_limit_clamped_to_max():
    client, search, _ = make_client()
    assert client.get("/feed", params={"limit": 500}).status_code == 200
    assert search.calls[-1]["limit"] == 100


def test_first_page_is_cached_but_cursor_pages_are_not():
    client, search, cache = make_client()
    client.get("/feed")
    client.get("/feed")  # 캐시 적중 — 검색 재호출 없음 (30s TTL, ADR-014)
    assert len(search.calls) == 1
    assert len(cache.store) == 1

    client.get("/feed", params={"cursor": CURSOR})
    client.get("/feed", params={"cursor": CURSOR})  # 커서 페이지는 캐시하지 않는다
    assert len(search.calls) == 3


class FakeGraph:
    """관계 근접도 스텁 — a_close만 가깝다."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def proximity(self, world_id, from_id, to_ids):
        self.calls.append((world_id, from_id, tuple(to_ids)))
        return {a: (0.8 if a == "a_close" else 0.0) for a in to_ids}


def make_personalized_client() -> tuple[TestClient, FakeSearch, FakeGraph, FakeCache]:
    search, cache, graph = FakeSearch(), FakeCache(), FakeGraph()
    # OS 밑점수는 a_far가 근소하게 앞서지만, 근접도(0.25×0.8)가 순서를 뒤집는다
    search.items = [
        {"event_id": "01JZK7Q3W0000000000000000B", "actor_id": "a_far", "_score": 0.50},
        {"event_id": "01JZK7Q3W0000000000000000A", "actor_id": "a_close", "_score": 0.45},
    ]
    cfg = Config(opensearch_url="http://unused", redis_url="redis://unused")
    app = create_app(cfg=cfg, search=search, cache=cache, graph=graph)
    return TestClient(app), search, graph, cache


def test_player_proximity_rescores_ranked_feed():
    client, search, graph, cache = make_personalized_client()
    resp = client.get("/feed", params={"player_id": "p_observer_0417", "limit": 2})
    body = resp.json()
    assert body["personalized"] is True
    # 관계 근접도가 순서를 뒤집는다: 0.45+0.25*0.8=0.65 > 0.50 (ADR-014 랭킹 공식)
    assert [i["actor_id"] for i in body["items"]] == ["a_close", "a_far"]
    assert graph.calls[0][1] == "p_observer_0417"
    # 후보 과확보: limit*2 요청
    assert search.calls[-1]["limit"] == 4
    # 개인화 응답은 공유 캐시에 남지 않는다
    assert cache.store == {}


def test_without_player_id_ranking_is_shared_and_cached():
    client, search, graph, cache = make_personalized_client()
    body = client.get("/feed", params={"limit": 2}).json()
    assert "personalized" not in body
    assert [i["actor_id"] for i in body["items"]] == ["a_far", "a_close"]  # OS 순서 유지
    assert graph.calls == []
    assert len(cache.store) == 1


def test_authorless_world_news_survives_personalization():
    # Director 세계 사건 포스트는 actor_id가 없다 — 근접도 항 없이 밑점수를 유지해야 한다
    client, search, graph, _ = make_personalized_client()
    search.items = [
        {"event_id": "01JZK7Q3W0000000000000000B", "actor_id": None, "_score": 0.60},
        {"event_id": "01JZK7Q3W0000000000000000A", "actor_id": "a_close", "_score": 0.45},
    ]
    body = client.get("/feed", params={"player_id": "p_observer_0417", "limit": 2}).json()
    assert body["personalized"] is True
    assert graph.calls[0][2] == ("a_close",)  # None은 근접도 질의에 실리지 않는다
    # 세계 뉴스(0.60) > a_close(0.45+0.25*0.8=0.65)? — 0.65가 앞선다: 정렬만 확인
    assert [i["event_id"][-1] for i in body["items"]] == ["A", "B"]


def test_search_delegates_with_resolved_actors():
    """GET /search — 검색어·역해석된 작성자 id가 그대로 질의로 위임된다."""
    client, search, _ = make_client()
    resp = client.get(
        "/search", params=[("q", "오디션"), ("actor", "a_aria_kim"), ("actor", "a_serin_yun")]
    )
    assert resp.status_code == 200
    assert resp.json()["query"] == "오디션"
    assert search.text_calls == [
        {
            "world_id": "w_main", "kinds": ["world"], "q": "오디션",
            "actor_ids": ["a_aria_kim", "a_serin_yun"], "limit": 20,
        }
    ]


def test_search_rejects_bad_actor_id_and_timeline_kinds():
    client, _, _ = make_client()
    # 작성자 id 형식이 아니면 400 — 임의 문자열이 OS 질의에 실리지 않는다
    assert client.get("/search", params={"q": "x", "actor": "김아리"}).status_code == 400
    # personal/private는 타임라인 경로다 — 검색 경로가 아니다
    assert client.get("/search", params={"q": "x", "types": "personal"}).status_code == 400
    # 검색어는 비어 있을 수 없다
    assert client.get("/search", params={"q": ""}).status_code == 422
