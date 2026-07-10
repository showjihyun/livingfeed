import pytest

from lf_dispatcher.subjects import dlq_subject, subject


def test_subject_format() -> None:
    assert (
        subject("dev", "w_main", "actor", "actor.action.performed")
        == "lf.dev.w_main.actor.action.performed"
    )


def test_stream_type_mismatch_rejected() -> None:
    with pytest.raises(ValueError):
        subject("dev", "w_main", "world", "actor.action.performed")


def test_unknown_stream_rejected() -> None:
    with pytest.raises(ValueError):
        subject("dev", "w_main", "emotion", "emotion.state.shifted")


def test_dlq_subject() -> None:
    original = "lf.dev.w_main.actor.action.performed"
    assert dlq_subject("dev", original) == "lf.dev.dlq.w_main.actor.action.performed"
