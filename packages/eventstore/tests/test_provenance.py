"""출처 집행 — 근거를 대지 못하는 이벤트는 세계에 들어올 수 없다 (ADR-021 §1).

여기 있는 것은 전부 순수 검증이라 DB가 필요 없다. 적재 경로의 집행 지점이
`store._validate`이므로, 그 함수를 직접 겨눈다 — append를 통해서만 검증하면
DB 미가동 환경에서 계약이 통째로 skip되어 회귀를 놓친다.
"""

from datetime import UTC, datetime

import pytest
from lf_eventstore import NewEvent, Provenance, ValidationFailed
from lf_eventstore.store import _build_envelope, _validate

OCCURRED = datetime(2026, 7, 28, tzinfo=UTC)

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
        provenance=Provenance.generated("t-0001"),
    )
    base.update(overrides)
    return NewEvent(**base)


def check(event: NewEvent) -> None:
    """적재 직전 검증과 동일한 경로 (append가 이 순서로 부른다)."""
    _validate("engine.actor", event, _build_envelope(event, OCCURRED))


# --- 팩토리: 등급과 근거가 짝을 이루는 것이 타입으로 보장된다 -------------------


def test_factories_carry_their_evidence() -> None:
    assert Provenance.recalled(["01JZK7Q3W0000000000000000G"]).kind == "recalled"
    assert Provenance.derived("emotion.appraise").rule_id == "emotion.appraise"
    assert Provenance.generated("t-1").trace_id == "t-1"
    assert Provenance.authored("p_observer").author_id == "p_observer"


@pytest.mark.parametrize(
    ("factory", "bad"),
    [
        (Provenance.recalled, []),  # 근거 사건 없는 '기억'
        (Provenance.derived, ""),
        (Provenance.generated, ""),
        (Provenance.authored, ""),
    ],
)
def test_factory_refuses_evidence_free_claim(factory, bad) -> None:
    with pytest.raises(ValueError):
        factory(bad)


def test_to_json_omits_irrelevant_evidence() -> None:
    """봉투는 해당 등급의 근거만 싣는다 (스키마가 additionalProperties를 막는다)."""
    assert Provenance.derived("goal.engine").to_json() == {
        "kind": "derived",
        "rule_id": "goal.engine",
    }


# --- 쓰기 경로 집행 -----------------------------------------------------------


def test_valid_event_passes() -> None:
    check(action_event())


def test_missing_provenance_rejected() -> None:
    with pytest.raises(ValidationFailed, match="provenance"):
        check(action_event(provenance=None))


def test_unknown_kind_rejected_on_write() -> None:
    """읽기에서만 유효한 값이다 — 새 이벤트가 미상을 자처할 수는 없다."""
    with pytest.raises(ValidationFailed, match="unknown"):
        check(action_event(provenance=Provenance(kind="unknown")))


@pytest.mark.parametrize(
    "provenance",
    [
        Provenance(kind="recalled"),  # 팩토리를 우회한 직접 생성
        Provenance(kind="derived"),
        Provenance(kind="generated"),
        Provenance(kind="authored"),
    ],
)
def test_evidence_free_provenance_rejected_by_schema(provenance: Provenance) -> None:
    """팩토리를 우회해도 스키마가 마지막 문을 잠근다."""
    with pytest.raises(ValidationFailed):
        check(action_event(provenance=provenance))


def test_provenance_rides_in_the_envelope() -> None:
    envelope = _build_envelope(action_event(), OCCURRED)
    assert envelope["provenance"] == {"kind": "generated", "trace_id": "t-0001"}


# --- 변환은 출처를 세탁하지 않는다 ---------------------------------------------


def test_inherit_keeps_generated_source() -> None:
    source = {"provenance": {"kind": "generated", "trace_id": "t-77"}}
    assert Provenance.inherit(source, rule_id="feed.compose:action") == Provenance.generated("t-77")


def test_inherit_keeps_authored_source() -> None:
    """스튜디오가 빚은 인물의 데뷔 포스트는 여전히 저작물이다 — 창발이 아니다."""
    source = {"provenance": {"kind": "authored", "author_id": "p_maker"}}
    got = Provenance.inherit(source, rule_id="feed.compose:debut")
    assert got == Provenance.authored("p_maker")


@pytest.mark.parametrize(
    "source_provenance",
    [
        {"kind": "derived", "rule_id": "goal.engine"},
        {"kind": "recalled", "source_event_ids": ["01JZK7Q3W0000000000000000G"]},
        {"kind": "unknown"},  # ADR-021 이전 적재분을 승격하는 경우
    ],
)
def test_inherit_falls_back_to_the_transform_rule(source_provenance: dict) -> None:
    """원본이 생성물도 저작물도 아니면, 변환 규칙 자체가 근거다."""
    got = Provenance.inherit({"provenance": source_provenance}, rule_id="feed.compose:goal")
    assert got == Provenance.derived("feed.compose:goal")


def test_inherit_survives_provenance_free_source() -> None:
    """리플레이·외부 주입 경로의 봉투에 출처가 없어도 승격은 멈추지 않는다."""
    assert Provenance.inherit({}, rule_id="feed.compose:incident") == Provenance.derived(
        "feed.compose:incident"
    )
