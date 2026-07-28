"""샘플 봉투는 정본 참조다 — 스키마와 어긋나면 문서가 거짓말을 한다.

샘플은 여러 엔진 테스트의 픽스처이자 새 이벤트를 만들 때 보고 베끼는 원본이다.
그래서 "스키마를 고쳤는데 샘플은 그대로"가 가장 조용히 번지는 드리프트다 —
봉투와 payload 양쪽을 실제 검증기로 통과시켜 그 틈을 막는다.
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from lf_schemas import registry

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"
SAMPLES = sorted(SAMPLES_DIR.glob("*.json"))


def test_samples_directory_is_found():
    """경로가 어긋나면 아래 테스트들이 조용히 0건으로 통과한다 — 빈 성공을 막는다."""
    assert SAMPLES, f"샘플을 찾지 못했다: {SAMPLES_DIR}"


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_sample_matches_envelope_and_payload_schema(path: Path):
    sample = json.loads(path.read_text(encoding="utf-8"))
    envelope_errors = [
        f"envelope {'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}"
        for e in Draft202012Validator(registry.envelope_schema()).iter_errors(sample)
    ]
    try:
        payload_schema = registry.payload_schema(sample["type"])
    except KeyError:
        pytest.fail(f"payload 스키마가 등록되지 않은 타입: {sample['type']}")
    payload_errors = [
        f"payload {'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}"
        for e in Draft202012Validator(payload_schema).iter_errors(sample["payload"])
    ]
    assert not (envelope_errors + payload_errors), "; ".join(envelope_errors + payload_errors)


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_sample_filename_matches_its_type(path: Path):
    """파일명이 곧 타입이라 테스트가 sample('actor.belief.formed')로 집어 온다."""
    sample = json.loads(path.read_text(encoding="utf-8"))
    assert path.name.startswith(f"{sample['type']}."), path.name


def test_every_event_type_has_a_sample():
    """새 이벤트를 만들면 샘플도 만든다 — 정본 없는 타입은 베낄 원본이 없다."""
    covered = {json.loads(p.read_text(encoding="utf-8"))["type"] for p in SAMPLES}
    missing = sorted(registry.known_event_types() - covered)
    assert not missing, f"샘플 없는 이벤트 타입: {missing}"
