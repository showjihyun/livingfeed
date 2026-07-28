"""append 경로 검증 — 실제 PostgreSQL 대상 (conftest 참고)."""

import re

import pytest
from lf_eventstore import (
    OUTBOX_CHANNEL,
    ConcurrencyConflict,
    NewEvent,
    PermissionDenied,
    Provenance,
    UnknownEventType,
    ValidationFailed,
    append,
    current_head,
    read_stream,
)

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

ACTION_PAYLOAD = {
    "action_kind": "speak",
    "intent": "테스트 발화",
    "target_actor_id": None,
    "location_id": None,
    "params": {},
    "decision_trace": {"trace_id": "t-0001", "tier": "hot"},
}


def action_event(**overrides) -> NewEvent:
    base = dict(
        world_id="w_test",
        stream="actor",
        stream_key="a_mint",
        type="actor.action.performed",
        tick=42,
        payload=ACTION_PAYLOAD,
        actor_id="a_mint",
        provenance=Provenance.generated(ACTION_PAYLOAD["decision_trace"]["trace_id"]),
    )
    base.update(overrides)
    return NewEvent(**base)


async def test_append_happy_path(conn):
    [stored] = await append(conn, "engine.actor", [action_event()], expected_head=0)

    assert stored.stream_seq == 1
    assert stored.global_seq >= 1
    env = stored.envelope
    assert ULID_RE.match(env["event_id"])
    assert env["correlation_id"] == env["event_id"]  # 사슬 시작 = 자기 자신
    assert env["payload"]["action_kind"] == "speak"

    assert await current_head(conn, "w_test", "actor", "a_mint") == 1
    cur = await conn.execute("SELECT count(*) FROM es.outbox WHERE published_at IS NULL")
    assert (await cur.fetchone())[0] == 1


async def test_append_multiple_and_continue(conn):
    stored = await append(
        conn, "engine.actor", [action_event(), action_event(tick=43)], expected_head=0
    )
    assert [s.stream_seq for s in stored] == [1, 2]

    [third] = await append(conn, "engine.actor", [action_event(tick=44)], expected_head=2)
    assert third.stream_seq == 3
    assert await current_head(conn, "w_test", "actor", "a_mint") == 3

    events = await read_stream(conn, "w_test", "actor", "a_mint")
    assert [e.stream_seq for e in events] == [1, 2, 3]
    assert [e.envelope["tick"] for e in events] == [42, 43, 44]
    assert all(e.envelope["actor_id"] == "a_mint" for e in events)  # 봉투 복원 (ADR-002)


async def test_provenance_survives_the_round_trip(conn):
    """출처는 봉투에 실려 나가고 컬럼에 남는다 — 둘이 갈리면 감사 체인이 끊긴다."""
    [stored] = await append(conn, "engine.actor", [action_event()], expected_head=0)
    expected = {"kind": "generated", "trace_id": "t-0001"}
    assert stored.envelope["provenance"] == expected

    [reread] = await read_stream(conn, "w_test", "actor", "a_mint")
    assert reread.envelope["provenance"] == expected

    # outbox 봉투(relay가 발행하는 것)와 events 컬럼이 같은 값을 갖는다
    cur = await conn.execute(
        "SELECT e.provenance, o.envelope -> 'provenance'"
        " FROM es.events e JOIN es.outbox o ON o.event_id = e.event_id"
    )
    assert list(await cur.fetchone()) == [expected, expected]


async def test_evidence_free_provenance_leaves_no_trace(conn):
    """근거 없는 출처 주장은 DB에 닿기 전에 거부된다 (ADR-021 §1)."""
    from lf_eventstore.model import Provenance as P

    with pytest.raises(ValidationFailed):
        await append(
            conn, "engine.actor", [action_event(provenance=P(kind="generated"))], expected_head=0
        )
    cur = await conn.execute("SELECT count(*) FROM es.events")
    assert (await cur.fetchone())[0] == 0


async def test_concurrency_conflict_on_new_stream(conn):
    await append(conn, "engine.actor", [action_event()], expected_head=0)
    with pytest.raises(ConcurrencyConflict):
        await append(conn, "engine.actor", [action_event()], expected_head=0)


async def test_concurrency_conflict_on_stale_head(conn):
    await append(conn, "engine.actor", [action_event()], expected_head=0)
    with pytest.raises(ConcurrencyConflict):
        await append(conn, "engine.actor", [action_event()], expected_head=5)
    # 실패한 append는 아무것도 남기지 않는다
    cur = await conn.execute("SELECT count(*) FROM es.events")
    assert (await cur.fetchone())[0] == 1


async def test_permission_denied_leaves_no_trace(conn):
    # Director는 actor.* 를 직접 쓸 수 없다 (ADR-013 hard rule)
    with pytest.raises(PermissionDenied):
        await append(conn, "engine.director", [action_event()], expected_head=0)
    cur = await conn.execute("SELECT count(*) FROM es.events")
    assert (await cur.fetchone())[0] == 0


async def test_unknown_event_type_rejected(conn):
    with pytest.raises(UnknownEventType):
        await append(
            conn, "engine.actor", [action_event(type="actor.unknown.happened")], expected_head=0
        )


async def test_payload_schema_violation_rejected(conn):
    bad = action_event(payload={"action_kind": "speak"})  # required 필드 누락
    with pytest.raises(ValidationFailed):
        await append(conn, "engine.actor", [bad], expected_head=0)


async def test_envelope_violation_rejected(conn):
    with pytest.raises(ValidationFailed):
        await append(conn, "engine.actor", [action_event(tick=-1)], expected_head=0)


async def test_stream_type_mismatch_rejected(conn):
    mismatched = action_event(stream="world")  # world 스트림에 actor.* 타입
    with pytest.raises(ValidationFailed):
        await append(conn, "engine.tick", [mismatched], expected_head=0)


async def test_mixed_streams_in_one_append_rejected(conn):
    with pytest.raises(ValueError):
        await append(
            conn,
            "engine.actor",
            [action_event(), action_event(stream_key="a_other")],
            expected_head=0,
        )


async def test_append_notifies_outbox_channel(conn):
    from psycopg import AsyncConnection

    from .conftest import DSN

    async with await AsyncConnection.connect(DSN, autocommit=True) as listener:
        await listener.execute(f"LISTEN {OUTBOX_CHANNEL}")
        [stored] = await append(conn, "engine.actor", [action_event()], expected_head=0)
        notifications = [n async for n in listener.notifies(timeout=5, stop_after=1)]

    assert len(notifications) == 1
    assert notifications[0].payload == str(stored.global_seq)
