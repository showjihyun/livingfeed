"""kuzu 무결성 검사 검증 — 원천(es) 대비 그래프 엣지 집합 (ADR-006 후속).

순수 비교 + (PG 게이트) 원천 적재→부분 프로젝션→누락 감지→복구→무결.
Kuzu는 임베디드(tmp_path)라 별도 인프라가 없다.
"""

from lf_eventstore import NewEvent, append
from lf_eventstore.migrate import migrate
from lf_projector.graph import RelGraph
from lf_projector.kuzu_verify import compare, verify_worlds

from .conftest import sample


def test_compare_reports_missing_and_extra():
    expected = {("a_x", "p_y"), ("p_y", "a_x")}
    actual = {("a_x", "p_y"), ("a_z", "p_y")}
    report = compare(expected, actual)
    assert not report["ok"]
    assert report["missing"] == ["p_y|a_x"]  # 프로젝션 누락 — rebuild 판단 근거
    assert report["extra"] == ["a_z|p_y"]
    assert compare(expected, set(expected))["ok"]
    assert compare(set(), set())["ok"]  # 빈 세계도 무결이다


async def test_verify_detects_projection_gap_and_recovery(pg, tmp_path):
    await pg.execute("DROP SCHEMA IF EXISTS es CASCADE")
    await migrate(pg)

    envelope = sample("relationship.state.changed")
    world = envelope["world_id"]
    payload = envelope["payload"]
    reverse = {**payload, "from_id": payload["to_id"], "to_id": payload["from_id"]}
    # 원천: 양방향 엣지의 이벤트를 es에 적재
    for p in (payload, reverse):
        await append(
            pg, "engine.relationship",
            [NewEvent(
                world_id=world, stream="relationship",
                stream_key=f"{p['from_id']}|{p['to_id']}",
                type="relationship.state.changed", tick=5,
                actor_id=p["from_id"], payload=p,
            )],
            expected_head=0,
        )

    graph = RelGraph(tmp_path)
    try:
        # 한 방향만 프로젝션됐다 — 누락을 감지해야 한다 (검사는 고치지 않는다)
        graph.apply_state_changed(world, {**envelope, "payload": payload})
        report = await verify_worlds(pg, graph, world_id=world)
        assert not report["ok"]
        assert report["worlds"][world]["missing"] == [
            f"{payload['to_id']}|{payload['from_id']}"
        ]
        assert report["worlds"][world]["extra"] == []

        # 남은 방향까지 프로젝션 — 무결 (세계 자동 열거 경로로도 확인)
        graph.apply_state_changed(world, {**envelope, "payload": reverse})
        assert (await verify_worlds(pg, graph))["ok"]
    finally:
        graph.close()


async def test_verify_sees_orphan_world_in_graph_only(pg, tmp_path):
    """원천에 없는 세계의 그래프 DB(고아 프로젝션)도 세계 열거에 잡혀야 한다."""
    await pg.execute("DROP SCHEMA IF EXISTS es CASCADE")
    await migrate(pg)

    graph = RelGraph(tmp_path)
    try:
        graph.apply_state_changed("w_ghost", sample("relationship.state.changed"))
        report = await verify_worlds(pg, graph)  # 자동 열거 — es는 비어 있다
        assert not report["ok"]
        assert report["worlds"]["w_ghost"]["extra"]  # 고아 엣지가 보인다
        assert report["worlds"]["w_ghost"]["missing"] == []
    finally:
        graph.close()
