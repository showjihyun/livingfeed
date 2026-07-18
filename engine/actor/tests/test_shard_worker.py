"""샤드 워커 분리 검증 (ADR-012 Phase 2) — 배리어·로스터 이원화·분산 tick.

핵심 계약:
- 실행 집합은 샤드별이지만 이름·유효 대상은 세계 전체다(로스터) — 타 샤드
  액터에게 말 걸기가 스크럽되지 않고, 화면 문장에 원시 id가 새지 않는다.
- 리더는 신호→자기 샤드 실행→ack 집계로 completed에 세계 전체 계수를 싣는다.
- 결번 샤드는 그 tick의 침묵이다 — 리더는 시한 후 진행한다 (미루기 정신).
"""

import asyncio
from datetime import UTC, datetime

from lf_actor.memory import WorkingMemory
from lf_actor.phases import ActorPhases
from lf_actor.shard_barrier import RedisShardBarrier
from lf_eventstore import read_stream
from lf_tick.clock import TickClock
from lf_tick.engine import _run_phases, run_tick
from lf_tick.pipeline import TickContext
from lf_tick.shard import shard_of
from psycopg import AsyncConnection

from .conftest import PG_DSN
from .test_social_loop import make_reader, make_writer  # 즉석 페르소나 조립 재사용

WORLD = "w_shard"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))
NUM_SHARDS = 2


class _SilentAi:
    async def decide_action(self, *args, **kwargs):
        return None

    async def converse(self, *args, **kwargs):
        return None

    async def reflect(self, *args, **kwargs):
        return None


def split_pair():
    """서로 다른 샤드에 떨어지는 페르소나 한 쌍 — 해싱이 갈라줄 때까지 재명명."""
    from dataclasses import replace

    reader, writer = make_reader(), make_writer()
    n = 0
    while shard_of(reader.id, NUM_SHARDS) == shard_of(writer.id, NUM_SHARDS):
        writer = replace(writer, id=f"{make_writer().id}_{n}")
        n += 1
        assert n < 32, "crc32가 32번 연속 같은 샤드를 줄 확률은 사실상 0이다"
    return reader, writer


async def test_barrier_signal_ack_roundtrip(redis):
    leader = RedisShardBarrier(redis, WORLD, own={0}, num_shards=NUM_SHARDS)
    follower = RedisShardBarrier(redis, WORLD, own={1}, num_shards=NUM_SHARDS)

    await leader.signal(42)
    assert await follower.next_signal(timeout_s=2) == 42

    await follower.ack(42, {"decided": {"hot": 1, "warm": 0, "cold": 0}, "emitted": 3})
    acks = await leader.wait_acks(42, timeout_s=2)
    assert acks == {1: {"decided": {"hot": 1, "warm": 0, "cold": 0}, "emitted": 3}}


async def test_barrier_drains_stale_signals_to_latest(redis):
    """다운됐다 복귀한 팔로워는 밀린 신호를 버리고 최신 tick만 따른다 (미루기 정신)."""
    leader = RedisShardBarrier(redis, WORLD, own={0}, num_shards=NUM_SHARDS)
    follower = RedisShardBarrier(redis, WORLD, own={1}, num_shards=NUM_SHARDS)
    for tick in (10, 11, 12):
        await leader.signal(tick)
    assert await follower.next_signal(timeout_s=2) == 12


async def test_barrier_timeout_reports_partial(redis):
    """결번 샤드는 그 tick의 침묵 — 리더는 시한 후 온 것만 들고 진행한다."""
    leader = RedisShardBarrier(redis, WORLD, own={0}, num_shards=NUM_SHARDS)
    acks = await leader.wait_acks(7, timeout_s=0.4)
    assert acks == {}


async def test_roster_spans_shards_but_execution_is_local(redis):
    """이름·유효 대상은 세계 전체, 실행은 내 샤드만 — 이원화의 계약."""
    reader, writer = split_pair()
    own = shard_of(reader.id, NUM_SHARDS)
    phases = ActorPhases(
        [reader, writer],
        ai=_SilentAi(),
        memory=WorkingMemory(redis),
        shard_select=lambda p: shard_of(p.id, NUM_SHARDS) == own,
    )
    assert set(phases._personas) == {reader.id}  # 실행 집합은 내 샤드
    assert set(phases._roster) == {reader.id, writer.id}  # 명부는 세계 전체
    assert phases._roster[writer.id] == writer.name  # 타 샤드도 실명 그라운딩


async def test_distributed_tick_aggregates_both_shards(conn, redis):
    """리더+팔로워 분산 tick — 두 샤드의 행동이 한 tick에 적재되고 계수가 합산된다."""
    reader, writer = split_pair()
    leader_shard = shard_of(reader.id, NUM_SHARDS)
    follower_shard = shard_of(writer.id, NUM_SHARDS)

    def phases_for(shard: int) -> ActorPhases:
        return ActorPhases(
            [reader, writer],
            ai=_SilentAi(),
            memory=WorkingMemory(redis),
            shard_select=lambda p: shard_of(p.id, NUM_SHARDS) == shard,
        )

    leader_barrier = RedisShardBarrier(
        redis, WORLD, own={leader_shard}, num_shards=NUM_SHARDS
    )
    follower_barrier = RedisShardBarrier(
        redis, WORLD, own={follower_shard}, num_shards=NUM_SHARDS
    )
    follower_phases = phases_for(follower_shard)

    async def follower_once() -> None:
        # 실제 배치처럼 팔로워는 전용 커넥션이다 — 한 커넥션의 동시 phases는
        # 트랜잭션 중첩 충돌을 낸다 (워커 = 프로세스 = 커넥션)
        async with await AsyncConnection.connect(
            PG_DSN, connect_timeout=3, autocommit=True
        ) as follower_conn:
            tick = await follower_barrier.next_signal(timeout_s=5)
            assert tick is not None
            ctx = TickContext(
                world_id=WORLD, tick=tick,
                world_time=CLOCK.world_time_at(tick), conn=follower_conn,
            )
            decided, emitted = await _run_phases(follower_phases, ctx)
            await follower_barrier.ack(tick, {"decided": decided, "emitted": emitted})

    follower_task = asyncio.create_task(follower_once())
    head = await run_tick(
        conn, phases_for(leader_shard), CLOCK, WORLD, 0, 0,
        barrier=leader_barrier, ack_timeout_s=10,
    )
    await asyncio.wait_for(follower_task, timeout=10)
    assert head == 2

    # 두 샤드의 액터가 같은 tick에 각자 행동을 남겼다 (규칙 폴백 — LLM 무관)
    for actor_id in (reader.id, writer.id):
        [action] = [
            s.envelope
            for s in await read_stream(conn, WORLD, "actor", actor_id)
            if s.envelope["type"] == "actor.action.performed"
        ]
        assert action["tick"] == 0

    # completed의 계수는 세계 전체다 — 리더 몫 + 팔로워 ack 합산
    events = await read_stream(conn, WORLD, "system", "tick")
    completed = events[-1].envelope
    assert completed["type"] == "system.tick.completed"
    total = completed["payload"]["actors_decided"]
    assert sum(total.values()) == 2
    assert completed["payload"]["events_emitted"] >= 2


async def test_leader_proceeds_without_dead_shard(conn, redis):
    """팔로워가 없어도 tick은 완주한다 — 결번 샤드는 그 tick의 침묵일 뿐."""
    reader, writer = split_pair()
    leader_shard = shard_of(reader.id, NUM_SHARDS)
    phases = ActorPhases(
        [reader, writer],
        ai=_SilentAi(),
        memory=WorkingMemory(redis),
        shard_select=lambda p: shard_of(p.id, NUM_SHARDS) == leader_shard,
    )
    barrier = RedisShardBarrier(
        redis, WORLD, own={leader_shard}, num_shards=NUM_SHARDS
    )
    head = await run_tick(
        conn, phases, CLOCK, WORLD, 0, 0, barrier=barrier, ack_timeout_s=0.5
    )
    assert head == 2
    events = await read_stream(conn, WORLD, "system", "tick")
    completed = events[-1].envelope
    assert sum(completed["payload"]["actors_decided"].values()) == 1  # 리더 몫만
