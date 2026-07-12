"""os-projector 문서 매핑·bulk 직렬화 검증 (ADR-014 §2단)."""

import json
from pathlib import Path

from lf_projector.os_index import MAPPING, bulk_body, envelope_to_doc

SAMPLE = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages" / "schemas" / "samples" / "feed.post.published.001.json"
    ).read_text(encoding="utf-8")
)


def test_envelope_to_doc_flattens_meta_and_payload():
    doc = envelope_to_doc(SAMPLE)
    assert doc["event_id"] == SAMPLE["event_id"]
    assert doc["world_id"] == "w_main"
    assert doc["visibility"] == "world"
    assert doc["title"] == SAMPLE["payload"]["title"]
    assert doc["drama_score"] == SAMPLE["payload"]["drama_score"]
    assert doc["correlation_id"] == SAMPLE["correlation_id"]


def test_doc_fields_are_all_in_strict_mapping():
    # dynamic:strict 인덱스는 매핑 밖 필드를 거부한다 — 문서와 매핑의 드리프트 방지
    doc = envelope_to_doc(SAMPLE)
    mapped = set(MAPPING["mappings"]["properties"])
    assert set(doc) <= mapped, f"매핑 누락 필드: {set(doc) - mapped}"


def test_bulk_body_is_ndjson_with_idempotent_ids():
    doc = envelope_to_doc(SAMPLE)
    body = bulk_body("lf-feed-posts", [doc])
    lines = body.strip().split("\n")
    assert len(lines) == 2
    action = json.loads(lines[0])
    # _id = event_id → 같은 이벤트 재적용은 덮어쓰기 (멱등, ADR-003 계약 1)
    assert action["index"]["_id"] == doc["event_id"]
    assert json.loads(lines[1]) == doc
    assert body.endswith("\n")
