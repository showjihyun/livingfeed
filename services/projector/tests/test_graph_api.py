"""graph query API 중재 검증 — world_graph op round-trip (ADR-006).

NATS 필요 (없으면 skip). kuzu는 임베디드라 temp 디렉터리로 실제 그래프를 쓴다.
subject는 env로 격리된다 — 상주 세계의 lf.dev.graph.query와 겹치지 않는다.
"""

import asyncio
import json
from pathlib import Path

import pytest
from lf_eventstore.testing import SKIP_NATS, assert_test_nats, test_nats_url
from lf_projector.graph import RelGraph, display_weight
from lf_projector.graph_api import GraphQueryClient, serve_graph_api

ENV = "test-worldgraph"
WORLD = "w_test"
NATS_URL = test_nats_url()

STATE_CHANGED = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages" / "schemas" / "samples" / "relationship.state.changed.001.json"
    ).read_text(encoding="utf-8")
)


@pytest.fixture
async def nc():
    """마커 검증된 테스트 NATS 연결 — 상주 세계면 assert_test_nats가 실패한다."""
    import nats

    if NATS_URL is None:
        pytest.skip(SKIP_NATS)
    connection = await asyncio.wait_for(nats.connect(NATS_URL, connect_timeout=3), timeout=5)
    try:
        await assert_test_nats(connection.jetstream())
        yield connection
    finally:
        await connection.close()


def state_changed(from_id, to_id, *, salience=0.1, **dims):
    envelope = json.loads(json.dumps(STATE_CHANGED))
    envelope["world_id"] = WORLD
    envelope["payload"] = {
        **envelope["payload"],
        "from_id": from_id, "to_id": to_id,
        "dimensions": {
            "trust": 0.0, "intimacy": 0.0, "respect": 0.0,
            "attraction": 0.0, "resentment": 0.0, **dims,
        },
        "salience": salience,
    }
    return envelope


async def test_world_graph_round_trip_applies_floor_and_cap(nc, tmp_path):
    graph = RelGraph(tmp_path / "kuzu")
    graph.apply_state_changed(WORLD, state_changed("a_x", "a_y", trust=0.5, intimacy=0.4))
    graph.apply_state_changed(WORLD, state_changed("a_m", "a_n", salience=0.05))  # 잔향뿐

    stop = asyncio.Event()
    server = asyncio.create_task(serve_graph_api(nc, graph, ENV, stop=stop))
    await asyncio.sleep(0.2)
    try:
        client = GraphQueryClient(nc, ENV)

        # 파라미터 미지정 — 바닥값 기본(프로젝터 단일 정의)이 잔향 간선을 거른다
        output = await client.world_graph(WORLD)
        [edge] = output["edges"]
        assert (edge["from_id"], edge["to_id"]) == ("a_x", "a_y")
        assert edge["weight"] == display_weight(0.5, 0.4, 0.1, 0.0)
        assert edge["dimensions"]["trust"] == pytest.approx(0.5)

        # 재정의 — 바닥값 0이면 둘 다, cap 1이면 강한 쪽만 남고 truncated
        assert len((await client.world_graph(WORLD, min_weight=0.0))["edges"]) == 2
        capped = await client.world_graph(WORLD, min_weight=0.0, limit=1)
        assert capped["truncated"] is True
        assert [e["from_id"] for e in capped["edges"]] == ["a_x"]
    finally:
        stop.set()
        await asyncio.wait_for(server, timeout=5)
        graph.close()


async def test_world_graph_absent_responder_degrades_to_none(nc):
    """응답자가 없으면 None — 호출측(feed-api)은 available=False로 강등한다."""
    client = GraphQueryClient(nc, "nosuch-worldgraph-env", timeout_s=0.3)
    assert await client.world_graph(WORLD) is None
