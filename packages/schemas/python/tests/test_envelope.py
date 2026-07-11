"""envelope 코드젠 산출물의 스모크 테스트 — 스키마↔모델 정합의 최소 검증."""

import pytest
from lf_schemas import EventEnvelope
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
