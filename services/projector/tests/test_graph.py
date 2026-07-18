"""Kuzu 관계 그래프 프로젝션 검증 — 임베디드라 인프라 없이 실제 DB로 테스트한다 (ADR-006)."""

import json
from pathlib import Path

import pytest
from lf_projector.graph import RelGraph, display_weight, strength

WORLD = "w_test"

ZERO_DIMS = {"trust": 0.0, "intimacy": 0.0, "respect": 0.0, "attraction": 0.0, "resentment": 0.0}

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


def state_changed(from_id, to_id, *, stage="acquaintance", salience=0.1, tick=50, **dims):
    """검증된 샘플 봉투에서 파생한 관계 변화 — 차원은 중립 기반에 부분 덮어쓰기."""
    envelope = json.loads(json.dumps(STATE_CHANGED))
    envelope["world_id"] = WORLD
    envelope["tick"] = tick
    envelope["payload"] = {
        **envelope["payload"],
        "from_id": from_id, "to_id": to_id,
        "dimensions": {**ZERO_DIMS, **dims},
        "stage": stage, "salience": salience,
    }
    return envelope


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


def test_world_graph_includes_actor_edges_with_direction_and_dimensions(graph):
    """세계 관계망 — 플레이어 간선만이 아니라 액터↔액터 간선도, 방향·차원 그대로."""
    envelope = json.loads(json.dumps(STATE_CHANGED))
    envelope["world_id"] = WORLD
    graph.apply_state_changed(WORLD, envelope)  # a_aria_kim → p_observer_0417
    graph.apply_state_changed(
        WORLD,
        state_changed("a_x", "a_y", trust=0.4, intimacy=0.3, salience=0.2, stage="friend"),
    )

    result = graph.world_graph(WORLD)
    assert result["truncated"] is False
    assert result["nodes"] == ["a_aria_kim", "a_x", "a_y", "p_observer_0417"]
    # 표시 가중치 내림차순 — 액터↔액터 간선이 먼저다
    first, second = result["edges"]
    assert (first["from_id"], first["to_id"]) == ("a_x", "a_y")  # 방향 보존
    assert first["stage"] == "friend"
    assert first["strength"] == strength(0.4, 0.3, 0.2)
    assert first["weight"] == display_weight(0.4, 0.3, 0.2, 0.0)
    assert first["dimensions"] == {**ZERO_DIMS, "trust": 0.4, "intimacy": 0.3}
    assert (second["from_id"], second["to_id"]) == ("a_aria_kim", "p_observer_0417")


def test_world_graph_floor_filters_weak_but_keeps_tension(graph):
    """바닥값은 무기록 잔향만 거른다 — 유대 없는 적대(긴장)도 관계망의 실측이다."""
    # 잔향뿐인 간선 (salience 0.05 → 가중치 0.01): 기본 바닥값 0.02 아래
    graph.apply_state_changed(WORLD, state_changed("a_m", "a_n", salience=0.05))
    # 적대 간선: strength는 0이지만 resentment가 표시 가중치를 끌어올린다
    graph.apply_state_changed(
        WORLD, state_changed("a_r", "a_s", resentment=0.6, salience=0.0, stage="rival")
    )

    result = graph.world_graph(WORLD)
    [edge] = result["edges"]
    assert (edge["from_id"], edge["to_id"]) == ("a_r", "a_s")
    assert edge["strength"] == 0.0
    assert edge["weight"] == 0.6  # display_weight = max(strength, resentment⁺)
    assert result["nodes"] == ["a_r", "a_s"]  # 걸러진 간선의 끝점은 노드에도 없다

    # 바닥값을 내리면 잔향 간선도 돌아온다 (재정의 가능)
    assert len(graph.world_graph(WORLD, min_weight=0.0)["edges"]) == 2


def test_world_graph_cap_keeps_strongest_and_flags_truncation(graph):
    graph.apply_state_changed(WORLD, state_changed("a_1", "a_2", intimacy=0.8))
    graph.apply_state_changed(WORLD, state_changed("a_3", "a_4", intimacy=0.5))
    graph.apply_state_changed(WORLD, state_changed("a_5", "a_6", intimacy=0.2))

    result = graph.world_graph(WORLD, limit=2)
    assert result["truncated"] is True
    assert [(e["from_id"], e["to_id"]) for e in result["edges"]] == [
        ("a_1", "a_2"), ("a_3", "a_4")
    ]
    assert result["nodes"] == ["a_1", "a_2", "a_3", "a_4"]  # 잘린 간선 끝점 미포함

    assert graph.world_graph(WORLD, limit=10)["truncated"] is False


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
