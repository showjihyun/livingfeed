"""이벤트 스토어 도메인 모델과 오류 (ADR-002/005)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class EventStoreError(Exception):
    """이벤트 스토어 오류의 공통 베이스."""


class ValidationFailed(EventStoreError):
    """봉투/payload 스키마 위반 — 적재 시점 트랜잭션 거부 (ADR-017 §2)."""


class UnknownEventType(ValidationFailed):
    """payload 스키마가 등록되지 않은 이벤트 타입."""


class PermissionDenied(ValidationFailed):
    """발행 권한 매트릭스 위반 (ADR-017 §2, permissions.yaml)."""


class ConcurrencyConflict(EventStoreError):
    """stream_heads 낙관적 잠금(CAS) 실패 — 호출자가 재수화 후 재시도한다 (ADR-005)."""


@dataclass(frozen=True)
class NewEvent:
    """append 요청 단위. event_id/occurred_at/correlation_id는 생략 시 생성된다.

    stream_key는 스트림 파티션 키(예: actor 스트림이면 actor_id)로,
    봉투에는 실리지 않고 es.events 컬럼에만 저장된다 (ADR-005).
    """

    world_id: str
    stream: str
    stream_key: str
    type: str
    tick: int
    payload: dict[str, Any] = field(default_factory=dict)
    actor_id: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    schema_version: int = 1
    event_id: str | None = None
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class StoredEvent:
    """적재 완료된 이벤트 — 전역/스트림 순번과 확정된 봉투."""

    global_seq: int
    stream_seq: int
    envelope: dict[str, Any]


@dataclass(frozen=True)
class OutboxRow:
    """relay가 발행할 outbox 행 (ADR-005 §Transactional Outbox)."""

    global_seq: int
    event_id: str
    envelope: dict[str, Any]
    enqueued_at: datetime
