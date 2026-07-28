"""세계 분기 — 반사실 실험의 정식 도구 (ADR-021 §4 L4).

가장 중요한 단정은 경계가 tick이 아니라 global_seq라는 것이다: 플레이어 개입은
tick 0 규약이라, tick으로 자르면 미래의 댓글이 과거의 분기에 들어앉는다.
"""

import pytest
from lf_eventstore import (
    ForkRefused,
    NewEvent,
    Provenance,
    ReplayTier,
    UnverifiableTier,
    append,
    assert_verifiable,
    current_head,
    fork_world,
    guarantee,
    read_stream,
)

SOURCE = "w_src"
TARGET = "w_fork"

TICK_PAYLOAD = {
    "tick": 0,
    "started_at": "2026-07-11T00:00:00Z",
    "completed_at": "2026-07-11T00:00:41Z",
    "duration_ms": 41_000,
    "actors_decided": {"hot": 1, "warm": 0, "cold": 0},
    "events_emitted": 0,
}


async def _tick(conn, tick: int, head: int) -> int:
    """tick 완료 이벤트 하나 — 분기 경계의 원천이다."""
    await append(
        conn, "engine.tick",
        [NewEvent(
            world_id=SOURCE, stream="system", stream_key="tick",
            type="system.tick.completed", tick=tick,
            provenance=Provenance.derived("tick.pipeline:completed"),
            payload={**TICK_PAYLOAD, "tick": tick},
        )],
        expected_head=head,
    )
    return head + 1


async def _comment(conn, text: str, head: int) -> int:
    """플레이어 개입 — **tick 0**이다 (session.py 규약). 경계 판정의 함정."""
    await append(
        conn, "services.gateway",
        [NewEvent(
            world_id=SOURCE, stream="player", stream_key="p_one",
            type="player.comment.posted", tick=0,
            provenance=Provenance.authored("p_one"),
            payload={
                "player_id": "p_one", "target_actor_id": "a_mint",
                "post_id": "01JZK7Q3W0000000000000000P", "text": text,
            },
        )],
        expected_head=head,
    )
    return head + 1


# --- 등급 계약 ---------------------------------------------------------------


def test_l3_refuses_to_be_verified():
    """보증하지 않는 등급에 초록불이 뜰 길 자체를 막는다 (ADR-021 §4)."""
    with pytest.raises(UnverifiableTier, match="L3"):
        assert_verifiable(ReplayTier.LLM_REEXECUTION)
    assert guarantee(ReplayTier.LLM_REEXECUTION).entry_point is None


@pytest.mark.parametrize(
    "tier",
    [ReplayTier.PLAYBACK, ReplayTier.REASSEMBLY, ReplayTier.RULE_REEXECUTION, ReplayTier.FORK],
)
def test_verifiable_tiers_name_where_they_are_enforced(tier: ReplayTier):
    """보증하는 등급은 집행 지점이 있어야 한다 — 없으면 말뿐인 보증이다."""
    spec = assert_verifiable(tier)
    assert spec.entry_point


# --- 분기 --------------------------------------------------------------------


async def test_fork_cuts_at_the_tick_boundary(conn):
    head = 0
    for tick in (1, 2, 3):
        head = await _tick(conn, tick, head)

    result = await fork_world(
        conn, source_world_id=SOURCE, target_world_id=TARGET, through_tick=2
    )
    assert result.events_copied == 2
    assert result.through_tick == 2

    ticks = await read_stream(conn, TARGET, "system", "tick")
    assert [s.envelope["tick"] for s in ticks] == [1, 2]  # tick 3은 분기 밖


async def test_player_interventions_are_cut_by_append_order_not_tick(conn):
    """tick 0 개입이 시점과 무관하게 딸려오면 미래가 과거에 들어앉는다."""
    head = 0
    head = await _tick(conn, 1, head)
    player_head = await _comment(conn, "분기 전에 남긴 말", 0)
    head = await _tick(conn, 2, head)
    await _comment(conn, "분기 후에 남긴 말", player_head)

    await fork_world(conn, source_world_id=SOURCE, target_world_id=TARGET, through_tick=2)

    comments = await read_stream(conn, TARGET, "player", "p_one")
    texts = [s.envelope["payload"]["text"] for s in comments]
    assert texts == ["분기 전에 남긴 말"]  # tick은 둘 다 0이지만 적재 순서가 갈랐다


async def test_fork_is_independent_of_its_source(conn):
    """분기 뒤의 발산이 측정 대상이다 — 한쪽 쓰기가 다른 쪽에 새면 실험이 성립 안 한다."""
    head = await _tick(conn, 1, 0)
    await fork_world(conn, source_world_id=SOURCE, target_world_id=TARGET, through_tick=1)

    await _tick(conn, 2, head)  # 원본만 계속 흐른다
    assert len(await read_stream(conn, SOURCE, "system", "tick")) == 2
    assert len(await read_stream(conn, TARGET, "system", "tick")) == 1

    # 분기 세계의 스트림 head가 사본에 맞춰 서 있다 — 이어서 적재할 수 있다
    assert await current_head(conn, TARGET, "system", "tick") == 1


async def test_fork_declares_itself(conn):
    """갈라진 세계가 스스로 분기라고 말해야 독립 세계로 오해되지 않는다."""
    await _tick(conn, 1, 0)
    result = await fork_world(
        conn, source_world_id=SOURCE, target_world_id=TARGET, through_tick=1
    )

    [declared] = await read_stream(conn, TARGET, "system", "fork")
    payload = declared.envelope["payload"]
    assert payload["source_world_id"] == SOURCE
    assert payload["boundary_global_seq"] == result.boundary_global_seq
    assert payload["events_copied"] == result.events_copied
    # 복사는 결정적 작업이다 — 사람이 눌렀을 뿐 내용을 지어내지 않았다
    assert declared.envelope["provenance"]["kind"] == "derived"


async def test_fork_does_not_republish_history(conn):
    """outbox에 넣으면 전 역사가 JetStream으로 다시 나가 살아 있는 프로젝션을 덮친다."""
    await _tick(conn, 1, 0)
    before = (await (await conn.execute("SELECT count(*) FROM es.outbox")).fetchone())[0]
    await fork_world(conn, source_world_id=SOURCE, target_world_id=TARGET, through_tick=1)
    after = (await (await conn.execute("SELECT count(*) FROM es.outbox")).fetchone())[0]
    assert after - before == 1  # 분기 선언 하나뿐 — 복사분은 발행되지 않는다


async def test_fork_refuses_a_world_that_already_has_history(conn):
    """남의 역사를 덧붙이면 두 세계가 한 스트림에서 섞이고 stream_seq가 겹친다."""
    await _tick(conn, 1, 0)
    await fork_world(conn, source_world_id=SOURCE, target_world_id=TARGET, through_tick=1)
    with pytest.raises(ForkRefused, match="이미 역사가 있다"):
        await fork_world(conn, source_world_id=SOURCE, target_world_id=TARGET, through_tick=1)


async def test_fork_refuses_without_a_completed_tick(conn):
    """분기점의 원천은 system.tick.completed다 — 없으면 자를 곳을 지어내지 않는다."""
    await _comment(conn, "tick이 하나도 안 끝난 세계", 0)
    with pytest.raises(ForkRefused, match="완료된 tick이 없다"):
        await fork_world(conn, source_world_id=SOURCE, target_world_id=TARGET, through_tick=9)


async def test_fork_refuses_itself(conn):
    with pytest.raises(ForkRefused):
        await fork_world(conn, source_world_id=SOURCE, target_world_id=SOURCE, through_tick=1)
