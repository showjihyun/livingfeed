"""GET /graph/relationships — graph query API 중재 검증 (ADR-006).

NATS 필요 (없으면 skip). kuzu는 임베디드라 temp 디렉터리로 실제 그래프를 쓴다.
"""

import asyncio
import json
from pathlib import Path

import httpx
from lf_gateway.config import Config
from lf_gateway.main import create_app
from lf_projector.graph import RelGraph, strength
from lf_projector.graph_api import serve_graph_api

ENV = "test"
WORLD = "w_main"
PLAYER = "p_observer_0417"

STATE_CHANGED = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages" / "schemas" / "samples" / "relationship.state.changed.001.json"
    ).read_text(encoding="utf-8")
)


async def test_graph_endpoint_returns_measured_strength(nc, tmp_path):
    graph = RelGraph(tmp_path / "kuzu")
    envelope = json.loads(json.dumps(STATE_CHANGED))
    envelope["world_id"] = WORLD
    graph.apply_state_changed(WORLD, envelope)

    stop = asyncio.Event()
    server = asyncio.create_task(serve_graph_api(nc, graph, ENV, stop=stop))
    await asyncio.sleep(0.2)
    try:
        app = create_app(cfg=Config(nats_url="unused(주입)", env=ENV), nc=nc)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            r = await client.get("/graph/relationships", params={"player_id": PLAYER})
            assert r.status_code == 200
            body = r.json()
            assert body["available"] is True
            [edge] = body["edges"]
            assert edge["actor_id"] == "a_aria_kim"
            assert edge["strength"] == strength(0.12, 0.09, 0.14)  # 실측값 (단일 정의)
            assert edge["stage"] == "acquaintance"

            assert (
                await client.get("/graph/relationships", params={"player_id": "bad!"})
            ).status_code == 422
    finally:
        stop.set()
        await asyncio.wait_for(server, timeout=5)
        graph.close()


async def test_graph_endpoint_degrades_when_api_absent(nc):
    """graph query 응답자가 없으면 available=False — 오류가 아니라 강등이다."""
    app = create_app(cfg=Config(nats_url="unused(주입)", env="nosuchenv"), nc=nc)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        body = (await client.get("/graph/relationships", params={"player_id": PLAYER})).json()
        assert body == {"player_id": PLAYER, "edges": [], "available": False}
