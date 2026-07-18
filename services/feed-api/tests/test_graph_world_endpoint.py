"""GET /graph/world 계약 검증 — 위임·파라미터 검증·미가용 강등 (ADR-006 중재)."""

from fastapi.testclient import TestClient
from lf_feed_api.config import Config
from lf_feed_api.main import create_app

WORLD_OUTPUT = {
    "nodes": ["a_x", "a_y", "p_observer_0417"],
    "edges": [
        {
            "from_id": "a_x", "to_id": "a_y",
            "strength": 0.31, "weight": 0.31, "stage": "friend",
            "dimensions": {
                "trust": 0.4, "intimacy": 0.3, "respect": 0.0,
                "attraction": 0.0, "resentment": 0.0,
            },
            "salience": 0.2, "updated_tick": 50,
        }
    ],
    "truncated": False,
}


class FakeGraph:
    """GraphQueryClient 스텁 — 위임 파라미터를 기록하고 정해진 출력을 돌려준다."""

    def __init__(self, output):
        self.output = output
        self.calls: list[dict] = []

    async def world_graph(self, world_id, *, min_weight=None, limit=None):
        self.calls.append({"world_id": world_id, "min_weight": min_weight, "limit": limit})
        return self.output


def make_client(output=WORLD_OUTPUT) -> tuple[TestClient, FakeGraph]:
    graph = FakeGraph(output)
    cfg = Config(opensearch_url="http://unused", redis_url="redis://unused")
    app = create_app(cfg=cfg, search=object(), cache=object(), graph=graph)
    return TestClient(app), graph


def test_world_graph_delegates_and_marks_available():
    client, graph = make_client()
    resp = client.get("/graph/world", params={"min_weight": 0.1, "limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["world_id"] == "w_main"
    assert body["nodes"] == WORLD_OUTPUT["nodes"]
    assert body["edges"] == WORLD_OUTPUT["edges"]
    assert body["truncated"] is False
    assert graph.calls == [{"world_id": "w_main", "min_weight": 0.1, "limit": 5}]


def test_world_graph_defaults_delegate_to_projector_single_definition():
    """바닥값·cap 미지정은 None으로 위임 — 기본값의 단일 정의는 프로젝터에 있다."""
    client, graph = make_client()
    assert client.get("/graph/world").status_code == 200
    assert graph.calls == [{"world_id": "w_main", "min_weight": None, "limit": None}]


def test_world_graph_validates_params():
    client, graph = make_client()
    assert client.get("/graph/world", params={"min_weight": 1.5}).status_code == 422
    assert client.get("/graph/world", params={"min_weight": -0.1}).status_code == 422
    assert client.get("/graph/world", params={"limit": 0}).status_code == 422
    assert client.get("/graph/world", params={"limit": 501}).status_code == 422
    assert client.get("/graph/world", params={"world_id": "bad!"}).status_code == 422
    assert graph.calls == []  # 검증 실패는 질의에 닿지 않는다


UNAVAILABLE = {
    "world_id": "w_main", "nodes": [], "edges": [], "truncated": False, "available": False,
}


def test_world_graph_degrades_when_query_api_absent():
    """graph query 응답자가 없으면(None) available=False — 오류가 아니라 강등이다."""
    client, _ = make_client(output=None)
    assert client.get("/graph/world").json() == UNAVAILABLE


def test_world_graph_degrades_when_client_missing():
    """NATS 미가용으로 클라이언트 자체가 없어도 같은 강등 — 읽기 경로는 죽지 않는다."""
    client, _ = make_client()
    client.app.state.graph = None
    assert client.get("/graph/world").json() == UNAVAILABLE
