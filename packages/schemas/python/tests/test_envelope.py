"""envelope 코드젠 산출물의 스모크 테스트 — 스키마↔모델 정합의 최소 검증."""

import pytest
from jsonschema import Draft202012Validator
from lf_schemas import EventEnvelope, registry
from pydantic import ValidationError

VALID = {
    "event_id": "01JZK7Q3W0000000000000000A",
    "stream": "actor",
    "type": "actor.action.performed",
    "schema_version": 1,
    "world_id": "w_main",
    "actor_id": "a_aria_kim",
    "tick": 42,
    "occurred_at": "2026-07-11T00:00:00Z",
    "causation_id": None,
    "correlation_id": "01JZK7Q3W0000000000000000B",
    "provenance": {"kind": "generated", "trace_id": "01JZK7Q3W0000000000000000C"},
    "payload": {},
}


def test_valid_envelope_parses() -> None:
    env = EventEnvelope.model_validate(VALID)
    assert env.stream.value == "actor"
    assert env.tick == 42


def test_bad_type_format_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate({**VALID, "type": "NotAValidType"})


def test_negative_tick_rejected() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate({**VALID, "tick": -1})


def test_provenance_is_required() -> None:
    """출처 없는 봉투는 봉투가 아니다 (ADR-021 §1)."""
    without = {k: v for k, v in VALID.items() if k != "provenance"}
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(without)


# --- 등급별 근거 필수 ---------------------------------------------------------
# 조건부 필수(if/then)를 집행하는 것은 **JSON Schema**다. datamodel-code-generator는
# if/then을 모델로 옮기지 않아 pydantic 모델은 이 규칙을 모른다 — 실제 적재 경로
# (store._validate)가 쓰는 것도 이 검증기이므로, 계약은 여기서 검증한다.
_ENVELOPE = Draft202012Validator(registry.envelope_schema())


def _errors(provenance: dict) -> list[str]:
    return [e.message for e in _ENVELOPE.iter_errors({**VALID, "provenance": provenance})]


@pytest.mark.parametrize(
    "provenance",
    [
        {"kind": "recalled", "source_event_ids": ["01JZK7Q3W0000000000000000G"]},
        {"kind": "derived", "rule_id": "emotion.appraise"},
        {"kind": "generated", "trace_id": "01JZK7Q3W0000000000000000C"},
        {"kind": "authored", "author_id": "p_observer_0417"},
    ],
)
def test_each_kind_with_its_evidence_passes(provenance: dict) -> None:
    assert _errors(provenance) == []


@pytest.mark.parametrize(
    "provenance",
    [
        {"kind": "recalled"},  # 근거 사건 없는 '기억'
        {"kind": "recalled", "source_event_ids": []},  # 빈 근거도 근거가 아니다
        {"kind": "derived"},  # 규칙 없는 '규칙 파생'
        {"kind": "generated"},  # 추적 불가한 생성물 — 가장 위험한 누락
        {"kind": "authored"},  # 저자 없는 '저작물'
    ],
)
def test_kind_without_evidence_rejected(provenance: dict) -> None:
    assert _errors(provenance), f"근거 없는 {provenance}가 통과했다"


def test_unknown_kind_is_readable() -> None:
    """ADR-021 이전 적재분을 읽을 수 있어야 리플레이가 성립한다 (쓰기는 store가 막는다)."""
    assert _errors({"kind": "unknown"}) == []


def test_undeclared_kind_rejected() -> None:
    assert _errors({"kind": "vibes"})
