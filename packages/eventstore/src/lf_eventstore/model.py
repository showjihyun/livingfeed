"""이벤트 스토어 도메인 모델과 오류 (ADR-002/005)."""

from __future__ import annotations

from collections.abc import Sequence
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


#: ADR-021 이전 적재분의 출처 — 읽기에서만 허용된다 (쓰기는 store._validate가 거부).
UNKNOWN_KIND = "unknown"


@dataclass(frozen=True)
class Provenance:
    """출처 등급 — 이 이벤트의 내용이 어디서 왔는가 (ADR-021 §1).

    등급마다 근거가 필수다. 근거 없는 출처 주장은 '최선 노력 감사 추적'이 되고,
    그건 연구용으로는 없는 것과 같다 — 소비자가 필드를 신뢰할 수 없기 때문이다.
    팩토리를 통해서만 만들면 등급과 근거가 짝을 이루는 것이 타입으로 보장된다.
    """

    kind: str
    source_event_ids: tuple[str, ...] | None = None
    rule_id: str | None = None
    trace_id: str | None = None
    author_id: str | None = None

    @classmethod
    def recalled(cls, source_event_ids: Sequence[str]) -> Provenance:
        """기존 사건에서 인출된 사실 — 근거 사건이 반드시 있다."""
        ids = tuple(source_event_ids)
        if not ids:
            raise ValueError("recalled은 근거 사건이 있어야 한다 — 없으면 기억이 아니다")
        return cls(kind="recalled", source_event_ids=ids)

    @classmethod
    def derived(cls, rule_id: str) -> Provenance:
        """규칙에서 결정적으로 파생 — rule_id가 L2 재실행의 진입점이다 (ADR-021 §4)."""
        if not rule_id:
            raise ValueError("derived는 rule_id가 있어야 한다")
        return cls(kind="derived", rule_id=rule_id)

    @classmethod
    def generated(cls, trace_id: str) -> Provenance:
        """LLM이 이번 호출에서 생성한 해석 — 재현 보증 없음, trace_id가 유일한 고리다."""
        if not trace_id:
            raise ValueError("generated는 trace_id가 있어야 한다 — 없으면 추적 불가한 생성물")
        return cls(kind="generated", trace_id=trace_id)

    @classmethod
    def authored(cls, author_id: str) -> Provenance:
        """사람이 저작 — 스튜디오 페르소나, 플레이어 개입, 시드 저작물."""
        if not author_id:
            raise ValueError("authored는 author_id가 있어야 한다")
        return cls(kind="authored", author_id=author_id)

    @classmethod
    def inherit(cls, source_envelope: dict[str, Any], *, rule_id: str) -> Provenance:
        """원본을 변환해 만든 파생 이벤트의 출처.

        **변환은 출처를 세탁하지 않는다.** 원본의 LLM 생성물이나 사람의 저작물이
        새 이벤트의 내용으로 흘러 들어가면, 그 내용은 여전히 생성물이고 저작물이다.
        규칙이 한 일은 옮겨 담은 것뿐이므로 규칙을 근거로 내세울 수 없다.
        원본이 규칙 파생·기억 인출이거나 출처 미상이면, 변환 규칙 자체가 근거가 된다.
        """
        source = source_envelope.get("provenance") or {}
        kind = source.get("kind")
        if kind == "generated" and source.get("trace_id"):
            return cls.generated(source["trace_id"])
        if kind == "authored" and source.get("author_id"):
            return cls.authored(source["author_id"])
        return cls.derived(rule_id)

    #: 등급 → 그 등급을 주장하려면 있어야 하는 근거 필드 (unknown은 근거 없음이 정의다)
    _EVIDENCE = {
        "recalled": "source_event_ids",
        "derived": "rule_id",
        "generated": "trace_id",
        "authored": "author_id",
    }

    def missing_evidence(self) -> str | None:
        """근거를 대지 못하면 그 필드 이름 — 댈 수 있으면 None.

        스키마(oneOf)도 같은 규칙을 집행하지만 oneOf의 위반 메시지는 "어느
        변형에도 맞지 않는다"로 뭉개진다. 생산자가 무엇을 빠뜨렸는지 바로
        알아야 하므로, 규칙의 원천은 여기 두고 store가 이 답을 먼저 쓴다.
        """
        field_name = self._EVIDENCE.get(self.kind)
        if field_name is None:
            return None
        return None if getattr(self, field_name) else field_name

    def to_json(self) -> dict[str, Any]:
        """봉투에 실리는 형태 — 해당 없는 근거 필드는 싣지 않는다."""
        data: dict[str, Any] = {"kind": self.kind}
        if self.source_event_ids is not None:
            data["source_event_ids"] = list(self.source_event_ids)
        if self.rule_id is not None:
            data["rule_id"] = self.rule_id
        if self.trace_id is not None:
            data["trace_id"] = self.trace_id
        if self.author_id is not None:
            data["author_id"] = self.author_id
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Provenance:
        """저장된 봉투에서 복원 — 검증은 하지 않는다 (읽기는 관대하다)."""
        ids = data.get("source_event_ids")
        return cls(
            kind=data.get("kind", UNKNOWN_KIND),
            source_event_ids=tuple(ids) if ids is not None else None,
            rule_id=data.get("rule_id"),
            trace_id=data.get("trace_id"),
            author_id=data.get("author_id"),
        )


@dataclass(frozen=True)
class NewEvent:
    """append 요청 단위. event_id/occurred_at/correlation_id는 생략 시 생성된다.

    stream_key는 스트림 파티션 키(예: actor 스트림이면 actor_id)로,
    봉투에는 실리지 않고 es.events 컬럼에만 저장된다 (ADR-005).

    provenance는 **필수**다 (ADR-021 §1). 기본값이 None인 것은 기존 호출부의
    위치 인자를 깨지 않기 위해서일 뿐이며, 생략하면 append가 거부한다 —
    출처를 대지 못하는 이벤트는 세계에 들어올 수 없다.
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
    provenance: Provenance | None = None


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
