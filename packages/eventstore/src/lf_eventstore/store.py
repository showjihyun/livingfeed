"""append — 세계 역사의 유일한 쓰기 경로 (ADR-002/005, ADR-017 §2).

한 번의 append는 단일 스트림(world_id, stream, stream_key)에 대한
낙관적 잠금 단위다: stream_heads CAS → events INSERT → outbox INSERT → NOTIFY
가 하나의 트랜잭션으로 묶인다. 검증 실패는 DB에 닿기 전에 거부된다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator
from lf_schemas import registry
from psycopg import AsyncConnection

from lf_eventstore.model import (
    UNKNOWN_KIND,
    ConcurrencyConflict,
    NewEvent,
    PermissionDenied,
    StoredEvent,
    UnknownEventType,
    ValidationFailed,
)
from lf_eventstore.ulid import new_ulid

#: outbox 웨이크업 채널 — relay가 LISTEN 한다 (ADR-005 §Transactional Outbox)
OUTBOX_CHANNEL = "lf_outbox"

_INSERT_EVENT = """
INSERT INTO es.events (
    event_id, world_id, stream, stream_key, stream_seq, type,
    schema_version, actor_id, tick, occurred_at, causation_id, correlation_id,
    provenance, payload
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
RETURNING global_seq
"""


def _isoformat_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _build_envelope(event: NewEvent, occurred_at: datetime) -> dict[str, Any]:
    event_id = event.event_id or new_ulid()
    envelope = {
        "event_id": event_id,
        "stream": event.stream,
        "type": event.type,
        "schema_version": event.schema_version,
        "world_id": event.world_id,
        "actor_id": event.actor_id,
        "tick": event.tick,
        "occurred_at": _isoformat_z(occurred_at),
        "causation_id": event.causation_id,
        # 인과 사슬의 시작이면 자기 자신이 correlation 루트다 (ADR-002 규칙 5)
        "correlation_id": event.correlation_id or event_id,
        "payload": event.payload,
    }
    # 미지정이면 키를 만들지 않는다 — _validate가 봉투 필수 위반으로 잡는다 (ADR-021 §1)
    if event.provenance is not None:
        envelope["provenance"] = event.provenance.to_json()
    return envelope


# 검증기는 스키마당 한 번만 컴파일한다 — Draft202012Validator 생성은 메타스키마
# 처리·$ref 해석을 매번 다시 하므로, append마다 새로 만들면 핫패스에 낭비가 쌓인다.
# 스키마 원천(lf_schemas.registry)이 @cache라 프로세스 수명 내 불변이므로 안전하다.
@cache
def _envelope_validator() -> Draft202012Validator:
    return Draft202012Validator(registry.envelope_schema())


@cache
def _payload_validator(event_type: str) -> Draft202012Validator:
    # KeyError(미등록 타입)는 캐시되지 않는다 — 호출자가 UnknownEventType으로 변환한다
    return Draft202012Validator(registry.payload_schema(event_type))


def _validate(principal: str, event: NewEvent, envelope: dict[str, Any]) -> None:
    if not event.type.startswith(f"{event.stream}."):
        raise ValidationFailed(
            f"이벤트 타입 '{event.type}'이 stream '{event.stream}'에 속하지 않는다"
        )
    # 출처 집행 (ADR-021 §1) — 스키마도 같은 규칙을 갖지만, 가장 흔한 두 위반은
    # 여기서 먼저 잡아 생산자가 무엇을 빠뜨렸는지 바로 알게 한다.
    if event.provenance is None:
        raise ValidationFailed(
            f"'{event.type}'에 provenance가 없다 — 출처를 대지 못하는 이벤트는"
            " 적재할 수 없다 (ADR-021 §1). Provenance.recalled/derived/generated/authored"
            " 중 이 내용이 실제로 온 곳을 고르십시오"
        )
    if event.provenance.kind == UNKNOWN_KIND:
        raise ValidationFailed(
            f"'{event.type}'의 provenance가 '{UNKNOWN_KIND}'다 — ADR-021 이전 적재분을"
            " 읽을 때만 쓰이는 값이며 새 이벤트에는 쓸 수 없다"
        )
    missing = event.provenance.missing_evidence()
    if missing is not None:
        raise ValidationFailed(
            f"'{event.type}'의 provenance가 '{event.provenance.kind}'인데 {missing}이(가)"
            " 없다 — 근거를 대지 못하는 출처 주장은 적재할 수 없다 (ADR-021 §1)"
        )
    if not registry.is_allowed(principal, event.type):
        raise PermissionDenied(
            f"principal '{principal}'은 '{event.type}'을 발행할 수 없다 (permissions.yaml)"
        )
    try:
        payload_validator = _payload_validator(event.type)
    except KeyError as e:
        raise UnknownEventType(str(e)) from None

    errors = [
        f"envelope: {'/'.join(map(str, err.absolute_path)) or '(root)'}: {err.message}"
        for err in _envelope_validator().iter_errors(envelope)
    ]
    errors += [
        f"payload: {'/'.join(map(str, err.absolute_path)) or '(root)'}: {err.message}"
        for err in payload_validator.iter_errors(event.payload)
    ]
    if errors:
        raise ValidationFailed(f"'{event.type}' 스키마 위반: " + "; ".join(errors))


async def append(
    conn: AsyncConnection,
    principal: str,
    events: Sequence[NewEvent],
    *,
    expected_head: int,
) -> list[StoredEvent]:
    """단일 스트림에 이벤트를 원자적으로 적재한다.

    expected_head: 호출자가 마지막으로 본 stream_seq (새 스트림이면 0).
    다르면 ConcurrencyConflict — 호출자가 재수화 후 재시도한다 (ADR-005).
    """
    if not events:
        raise ValueError("적재할 이벤트가 없다")
    if expected_head < 0:
        raise ValueError("expected_head는 0 이상이어야 한다")

    stream_id = (events[0].world_id, events[0].stream, events[0].stream_key)
    for e in events:
        if (e.world_id, e.stream, e.stream_key) != stream_id:
            raise ValueError("한 번의 append는 단일 스트림만 대상으로 한다 (낙관적 잠금 단위)")

    prepared: list[tuple[NewEvent, dict[str, Any], datetime]] = []
    for e in events:
        occurred_at = e.occurred_at or datetime.now(UTC)
        envelope = _build_envelope(e, occurred_at)
        _validate(principal, e, envelope)
        prepared.append((e, envelope, occurred_at))

    world_id, stream, stream_key = stream_id
    new_head = expected_head + len(events)

    async with conn.transaction():
        if expected_head == 0:
            cur = await conn.execute(
                """
                INSERT INTO es.stream_heads (world_id, stream, stream_key, head_seq)
                VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
                """,
                (world_id, stream, stream_key, new_head),
            )
        else:
            cur = await conn.execute(
                """
                UPDATE es.stream_heads SET head_seq = %s
                WHERE world_id = %s AND stream = %s AND stream_key = %s AND head_seq = %s
                """,
                (new_head, world_id, stream, stream_key, expected_head),
            )
        if cur.rowcount == 0:
            raise ConcurrencyConflict(
                f"스트림 {stream_id} head가 {expected_head}가 아니다 — 재수화 후 재시도"
            )

        stored: list[StoredEvent] = []
        for i, (e, envelope, occurred_at) in enumerate(prepared):
            stream_seq = expected_head + 1 + i
            cur = await conn.execute(
                _INSERT_EVENT,
                (
                    envelope["event_id"], world_id, stream, stream_key, stream_seq,
                    e.type, e.schema_version, e.actor_id, e.tick, occurred_at,
                    envelope["causation_id"], envelope["correlation_id"],
                    json.dumps(envelope["provenance"], ensure_ascii=False),
                    json.dumps(e.payload, ensure_ascii=False),
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            global_seq: int = row[0]
            await conn.execute(
                "INSERT INTO es.outbox (global_seq, event_id, envelope) VALUES (%s, %s, %s::jsonb)",
                (global_seq, envelope["event_id"], json.dumps(envelope, ensure_ascii=False)),
            )
            stored.append(
                StoredEvent(global_seq=global_seq, stream_seq=stream_seq, envelope=envelope)
            )

        # relay 웨이크업 — 커밋 시점에 전달된다 (ADR-005 §outbox relay 지연)
        await conn.execute(
            "SELECT pg_notify(%s, %s)", (OUTBOX_CHANNEL, str(stored[-1].global_seq))
        )

    return stored


async def current_head(
    conn: AsyncConnection, world_id: str, stream: str, stream_key: str
) -> int:
    """스트림의 현재 head_seq (없으면 0)."""
    cur = await conn.execute(
        "SELECT head_seq FROM es.stream_heads"
        " WHERE world_id = %s AND stream = %s AND stream_key = %s",
        (world_id, stream, stream_key),
    )
    row = await cur.fetchone()
    return 0 if row is None else int(row[0])


async def read_stream(
    conn: AsyncConnection,
    world_id: str,
    stream: str,
    stream_key: str,
    *,
    from_seq: int = 1,
    limit: int | None = None,
) -> list[StoredEvent]:
    """스트림 이벤트를 stream_seq 순으로 읽는다 — 재수화(rehydration) 경로 (ADR-002).

    재수화는 스트림 전체가 필요하므로 limit 기본은 None(전체)이다. from_seq(키셋)와
    함께 쓰면 긴 스트림을 배치로 되감을 수 있다 — '마지막 N개 미리보기'처럼 창만
    필요한 호출자는 limit로 조회를 명시적으로 잡는다 (무제한 조회 방어).
    """
    cur = await conn.execute(
        """
        SELECT global_seq, stream_seq, event_id, type, schema_version, actor_id, tick,
               occurred_at, causation_id, correlation_id, provenance, payload
        FROM es.events
        WHERE world_id = %s AND stream = %s AND stream_key = %s AND stream_seq >= %s
        ORDER BY stream_seq
        LIMIT %s
        """,
        (world_id, stream, stream_key, from_seq, limit),
    )
    result: list[StoredEvent] = []
    async for row in cur:
        envelope = {
            "event_id": row[2],
            "stream": stream,
            "type": row[3],
            "schema_version": row[4],
            "world_id": world_id,
            "actor_id": row[5],
            "tick": row[6],
            "occurred_at": _isoformat_z(row[7]),
            "causation_id": row[8],
            "correlation_id": row[9],
            "provenance": row[10],
            "payload": row[11],
        }
        result.append(StoredEvent(global_seq=row[0], stream_seq=row[1], envelope=envelope))
    return result
