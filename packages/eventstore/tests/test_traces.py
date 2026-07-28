"""결정 트레이스 저장·보존 — ADR-021 §2/§5.

정책 판정(TracePolicy)은 순수 함수라 DB 없이 돌고, 저장·조회·정리는 실제 PG를
겨눈다 (conftest의 conn 픽스처).
"""

from datetime import UTC, datetime, timedelta

import pytest
from lf_eventstore import DecisionTrace, TracePolicy, purge_expired, read_trace, store_trace
from lf_eventstore.traces import (
    DEFAULT_RETENTION,
    DEFAULT_SAMPLE_RATE,
    RESEARCH_RETENTION,
)

NOW = datetime(2026, 7, 28, tzinfo=UTC)


def trace(trace_id: str = "01JZK7Q3W0000000000000000C") -> DecisionTrace:
    return DecisionTrace(
        trace_id=trace_id,
        world_id="w_test",
        actor_id="a_mint",
        tick=42,
        purpose="decide_action",
        system_prompt="당신은 '민트'다",
        user_prompt="## 작업 기억\n- tick 41: 나는 work",
        output='{"action_kind": "speak"}',
        model="claude-sonnet-5",
    )


# --- 정책: 결정적 샘플링 -------------------------------------------------------


def test_default_mode_is_the_cheap_one():
    """기본값은 싼 쪽이어야 한다 — 연구 모드는 명시적 옵트인이다 (ADR-021 §5)."""
    policy = TracePolicy()
    assert policy.sample_rate == DEFAULT_SAMPLE_RATE
    assert policy.retention == DEFAULT_RETENTION
    assert TracePolicy.research().retention == RESEARCH_RETENTION


def test_research_mode_keeps_everything():
    policy = TracePolicy.for_mode(research=True)
    assert all(policy.retains(f"trace-{i}") for i in range(200))


def test_zero_rate_keeps_nothing():
    policy = TracePolicy(sample_rate=0.0)
    assert not any(policy.retains(f"trace-{i}") for i in range(200))


def test_sampling_is_deterministic_not_random():
    """난수 샘플링이면 리플레이마다 남는 트레이스가 달라져 결정 기록이 흔들린다."""
    policy = TracePolicy(sample_rate=0.5)
    first = [policy.retains(f"trace-{i}") for i in range(500)]
    second = [TracePolicy(sample_rate=0.5).retains(f"trace-{i}") for i in range(500)]
    assert first == second
    # 그리고 실제로 갈라야 한다 — 전부 같은 답이면 샘플링이 아니다
    assert 0 < sum(first) < 500


def test_sampling_rate_is_roughly_honored():
    policy = TracePolicy(sample_rate=0.1)
    kept = sum(policy.retains(f"trace-{i}") for i in range(5_000))
    assert 350 < kept < 650  # 10% ± 여유 — 해시 분포의 흔들림 허용


# --- 저장·조회·정리 (실제 PG) --------------------------------------------------


async def test_stored_trace_round_trips(conn):
    policy = TracePolicy.research()
    assert await store_trace(conn, trace(), policy, now=NOW) is True

    got = await read_trace(conn, "01JZK7Q3W0000000000000000C")
    assert got is not None
    assert got["system_prompt"] == "당신은 '민트'다"
    assert got["purpose"] == "decide_action"
    assert got["expires_at"] == NOW + RESEARCH_RETENTION


async def test_unsampled_trace_is_not_stored(conn):
    """정책이 거르면 아무것도 남지 않는다 — 반환값이 곧 trace_retained다."""
    assert await store_trace(conn, trace(), TracePolicy(sample_rate=0.0), now=NOW) is False
    assert await read_trace(conn, "01JZK7Q3W0000000000000000C") is None


async def test_restoring_the_same_trace_does_not_extend_its_life(conn):
    """리플레이·재시도가 원문을 덮어써 기한만 늘리는 일이 없어야 한다."""
    policy = TracePolicy.research()
    await store_trace(conn, trace(), policy, now=NOW)
    later = NOW + timedelta(days=5)
    await store_trace(conn, trace(), policy, now=later)

    got = await read_trace(conn, "01JZK7Q3W0000000000000000C")
    assert got["expires_at"] == NOW + RESEARCH_RETENTION  # 첫 적재의 기한 그대로


async def test_purge_removes_only_expired(conn):
    policy = TracePolicy.research()
    await store_trace(conn, trace("01JZK7Q3W0000000000000000A"), policy, now=NOW)
    await store_trace(
        conn, trace("01JZK7Q3W0000000000000000B"), policy, now=NOW + timedelta(days=20)
    )

    # 첫 건만 기한이 지난 시점
    removed = await purge_expired(conn, now=NOW + RESEARCH_RETENTION + timedelta(seconds=1))
    assert removed == 1
    assert await read_trace(conn, "01JZK7Q3W0000000000000000A") is None
    assert await read_trace(conn, "01JZK7Q3W0000000000000000B") is not None


async def test_purge_is_batched(conn):
    """무제한 DELETE는 긴 트랜잭션·잠금을 만든다 — 배치가 반복 호출한다."""
    policy = TracePolicy.research()
    for i in range(5):
        await store_trace(conn, trace(f"01JZK7Q3W000000000000000{i:02X}"), policy, now=NOW)

    after = NOW + RESEARCH_RETENTION + timedelta(seconds=1)
    assert await purge_expired(conn, now=after, limit=2) == 2
    assert await purge_expired(conn, now=after, limit=2) == 2
    assert await purge_expired(conn, now=after, limit=2) == 1
    assert await purge_expired(conn, now=after, limit=2) == 0


@pytest.mark.parametrize("research", [False, True])
def test_for_mode_picks_the_matching_policy(research: bool):
    policy = TracePolicy.for_mode(research=research)
    assert (policy.retention == RESEARCH_RETENTION) is research
