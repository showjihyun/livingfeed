"""ULID 커서 디코딩 검증 — 커서 재개의 전제 (ADR-010)."""

import time

import pytest
from lf_eventstore import new_ulid
from lf_gateway.cursor import resume_start_time, ulid_timestamp_ms

# ULID 스펙 문서의 정본 예시 — Crockford Base32 정의로 1469922850259ms
# (= 2016-07-30T23:54:10.259Z)
CANONICAL = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def test_canonical_ulid_timestamp():
    assert ulid_timestamp_ms(CANONICAL) == 1469922850259


def test_roundtrip_with_eventstore_generator():
    # 커서(post id)는 lf-eventstore가 만든다 — 인코더와 디코더가 같은 시계를 봐야 한다
    before_ms = int(time.time() * 1000)
    decoded = ulid_timestamp_ms(new_ulid())
    assert abs(decoded - before_ms) < 5_000


def test_zero_ulid_is_epoch():
    assert ulid_timestamp_ms("0" * 26) == 0


def test_invalid_ulid_rejected():
    with pytest.raises(ValueError):
        ulid_timestamp_ms("not-a-ulid")
    with pytest.raises(ValueError):
        ulid_timestamp_ms("01ARZ3NDEKTSV4RRFFQ69G5FA")  # 25자


def test_resume_start_time_rewinds_by_slack():
    exact = resume_start_time(CANONICAL, slack_s=0)
    rewound = resume_start_time(CANONICAL, slack_s=60)
    assert exact == "2016-07-30T23:54:10.259000Z"
    assert rewound == "2016-07-30T23:53:10.259000Z"
