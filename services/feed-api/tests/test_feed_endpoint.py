"""GET /feed 계약 검증 — 파라미터 파싱, 커서 규칙, 캐시 (ADR-014 §2단)."""

from fastapi.testclient import TestClient
from lf_feed_api.config import Config
from lf_feed_api.main import create_app

CURSOR = "01JZK7Q3W0000000000000000A"


class FakeSearch:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.items = [{"event_id": CURSOR, "title": "t"}]

    async def search(self, world_id, kinds, *, limit, sort, cursor):
        self.calls.append(
            {"world_id": world_id, "kinds": kinds, "limit": limit, "sort": sort, "cursor": cursor}
        )
        next_cursor = self.items[-1]["event_id"] if sort == "recent" else None
        return {"items": self.items, "next_cursor": next_cursor, "mode": sort}


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
    assert client.get("/feed", params={"types": "personal, world"}).status_code == 200
    assert search.calls[-1]["kinds"] == ["personal", "world"]

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
