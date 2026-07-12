"""연결별 필터·SSE 프레이밍 검증 (ADR-010)."""

import json
from pathlib import Path

from lf_gateway.feed_stream import FeedFilter, parse_feed_event, sse_event

SAMPLE = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages" / "schemas" / "samples" / "feed.post.published.001.json"
    ).read_text(encoding="utf-8")
)


def flt(**kwargs) -> FeedFilter:
    defaults = {"world_id": "w_main", "kinds": frozenset({"world"}), "cursor": None}
    return FeedFilter(**{**defaults, **kwargs})


def test_accepts_matching_visibility():
    assert flt().accepts(SAMPLE)
    assert not flt(kinds=frozenset({"personal"})).accepts(SAMPLE)


def test_cursor_drops_already_seen_events():
    # BY_START_TIME 재개는 slack만큼 과거에서 시작한다 — 커서 이하 중복 제거가 필수다
    assert not flt(cursor=SAMPLE["event_id"]).accepts(SAMPLE)
    older = "0" * 26
    assert flt(cursor=older).accepts(SAMPLE)


def test_parse_feed_event_passes_raw_json_through():
    raw = json.dumps(SAMPLE, ensure_ascii=False).encode()
    parsed = parse_feed_event(raw, flt())
    assert parsed is not None
    event_id, event_type, data = parsed
    assert event_id == SAMPLE["event_id"]
    assert event_type == "feed.post.published"
    assert json.loads(data) == SAMPLE

    assert parse_feed_event(raw, flt(kinds=frozenset({"hidden"}))) is None


def test_sse_frame_uses_post_id_as_event_id():
    frame = sse_event("01ARZ3NDEKTSV4RRFFQ69G5FAV", "feed.post.published", "{}")
    assert frame == (
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nevent: feed.post.published\ndata: {}\n\n"
    )
