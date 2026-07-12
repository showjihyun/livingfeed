"""Kuzu 관계 그래프 프로젝션 검증 — 임베디드라 인프라 없이 실제 DB로 테스트한다 (ADR-006)."""

import json
from pathlib import Path

import pytest
from lf_projector.graph import RelGraph, strength

WORLD = "w_test"

STATE_CHANGED = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages" / "schemas" / "samples" / "relationship.state.changed.001.json"
    ).read_text(encoding="utf-8")
)
MILESTONE = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages" / "schemas" / "samples" / "relationship.milestone.reached.001.json"
    ).read_text(encoding="utf-8")
)


@pytest.fixture
def graph(tmp_path):
    g = RelGraph(tmp_path / "kuzu")
    yield g
    g.close()


def test_strength_formula_is_shared_definition():
    assert strength(trust=0.0, intimacy=0.0, salience=0.0) == 0.0
    assert strength(trust=1.0, intimacy=1.0, salience=1.0) == 1.0
    # 불신(trust<0)은 관계도를 깎지 않는다 — 애증도 강한 관계다
    assert strength(trust=-0.8, intimacy=0.4, salience=0.2) == strength(0.0, 0.4, 0.2)


def test_state_changed_projects_edge_idempotently(graph):
    envelope = json.loads(json.dumps(STATE_CHANGED))
    envelope["world_id"] = WORLD
    graph.apply_state_changed(WORLD, envelope)
    graph.apply_state_changed(WORLD, envelope)  # 재적용 — 멱등 (ADR-003 계약 1)

    result = graph.player_graph(WORLD, "p_observer_0417")
    [edge] = result["edges"]
    assert edge["actor_id"] == "a_aria_kim"
    assert edge["stage"] == "acquaintance"
    assert edge["dimensions"]["trust"] == pytest.approx(0.12)
    assert edge["strength"] == strength(0.12, 0.09, 0.14)


def test_milestone_before_state_creates_neutral_edge(graph):
    envelope = json.loads(json.dumps(MILESTONE))
    envelope["world_id"] = WORLD
    graph.apply_milestone(WORLD, envelope)

    [edge] = graph.player_graph(WORLD, "p_observer_0417")["edges"]
    assert edge["stage"] == "acquaintance"
    assert edge["dimensions"]["trust"] == 0.0

    # 이후 state.changed가 차원을 채운다
    changed = json.loads(json.dumps(STATE_CHANGED))
    changed["world_id"] = WORLD
    graph.apply_state_changed(WORLD, changed)
    [edge] = graph.player_graph(WORLD, "p_observer_0417")["edges"]
    assert edge["dimensions"]["intimacy"] == pytest.approx(0.09)


def test_proximity_uses_direct_edge_both_directions(graph):
    envelope = json.loads(json.dumps(STATE_CHANGED))
    envelope["world_id"] = WORLD
    graph.apply_state_changed(WORLD, envelope)

    # 엣지는 a_aria_kim→p_observer 방향뿐이지만 근접도는 양방향에서 찾는다
    scores = graph.proximity(WORLD, "p_observer_0417", ["a_aria_kim", "a_nobody"])
    assert scores["a_aria_kim"] == strength(0.12, 0.09, 0.14)
    assert scores["a_nobody"] == 0.0
    assert graph.proximity(WORLD, "p_observer_0417", ["p_observer_0417"]) == {
        "p_observer_0417": 1.0
    }


def test_worlds_are_isolated(graph):
    envelope = json.loads(json.dumps(STATE_CHANGED))
    envelope["world_id"] = WORLD
    graph.apply_state_changed(WORLD, envelope)
    assert graph.player_graph("w_other", "p_observer_0417")["edges"] == []


def test_drop_world_rebuild_path(graph):
    envelope = json.loads(json.dumps(STATE_CHANGED))
    envelope["world_id"] = WORLD
    graph.apply_state_changed(WORLD, envelope)
    graph.drop_world(WORLD)
    assert graph.player_graph(WORLD, "p_observer_0417")["edges"] == []  # 파괴 후 빈 그래프
