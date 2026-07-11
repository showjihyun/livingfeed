#!/usr/bin/env python
"""스키마 하위 호환성 게이트 (ADR-017 §5, ADR-001).

기존 샘플 이벤트(samples/*.json)를 현재 스키마로 재검증한다.
스키마 변경이 기존 이벤트를 깨뜨리면(예: required 필드 추가) 여기서 실패한다 —
breaking 변경은 schema_version 증가 + 업캐스터 등록으로만 가능하다 (ADR-002/017).

검사 항목:
  1. 모든 샘플이 envelope 스키마를 통과한다
  2. 모든 샘플의 payload가 events/<type>.schema.json 을 통과한다
  3. 모든 payload 스키마에 샘플이 1개 이상 존재한다 (새 이벤트 타입은 샘플 필수)
  4. permissions.yaml 구조·패턴 문법·stream 접두가 유효하다 (ADR-017 §2)

실행 (저장소 루트에서):
    uv run --package lf-schemas python packages/schemas/scripts/check_compat.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

SCHEMAS_ROOT = Path(__file__).resolve().parents[1]
EVENTS_DIR = SCHEMAS_ROOT / "events"
SAMPLES_DIR = SCHEMAS_ROOT / "samples"
PERMISSIONS_FILE = SCHEMAS_ROOT / "permissions.yaml"

# 허용 패턴: 정확한 타입("actor.action.performed") 또는 접두 와일드카드("actor.*")
PERMISSION_PATTERN = re.compile(r"^[a-z_]+(\.[a-z_]+)*(\.\*)?$")


def load_json(path: Path) -> dict:
    # utf-8-sig: Windows 도구가 붙이는 BOM을 허용
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate(instance: dict, schema: dict, label: str, errors: list[str]) -> None:
    for err in Draft202012Validator(schema).iter_errors(instance):
        where = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{label}: {where}: {err.message}")


def check_samples(errors: list[str]) -> None:
    envelope_schema = load_json(EVENTS_DIR / "envelope.schema.json")
    payload_schemas = {
        f.name.removesuffix(".schema.json"): load_json(f)
        for f in sorted(EVENTS_DIR.glob("*.schema.json"))
        if f.name != "envelope.schema.json"
    }

    covered: set[str] = set()
    sample_files = sorted(SAMPLES_DIR.glob("*.json"))
    if not sample_files:
        errors.append("samples/: 샘플 이벤트가 없다 — 게이트가 무의미해진다")
        return

    for sample_file in sample_files:
        label = f"samples/{sample_file.name}"
        try:
            event = load_json(sample_file)
        except json.JSONDecodeError as e:
            errors.append(f"{label}: JSON 파싱 실패 — {e}")
            continue

        validate(event, envelope_schema, f"{label} [envelope]", errors)

        event_type = event.get("type")
        if not isinstance(event_type, str):
            continue
        payload_schema = payload_schemas.get(event_type)
        if payload_schema is None:
            errors.append(
                f"{label}: 알 수 없는 이벤트 타입 '{event_type}' — "
                f"events/{event_type}.schema.json 이 없다"
            )
            continue
        covered.add(event_type)
        validate(event.get("payload", {}), payload_schema, f"{label} [payload]", errors)

    for missing in sorted(set(payload_schemas) - covered):
        errors.append(
            f"events/{missing}.schema.json: 샘플 이벤트가 없다 — "
            f"samples/{missing}.*.json 을 추가하라 (새 이벤트 타입은 샘플 필수)"
        )


def check_permissions(errors: list[str]) -> None:
    envelope_schema = load_json(EVENTS_DIR / "envelope.schema.json")
    valid_streams: set[str] = set(envelope_schema["properties"]["stream"]["enum"])

    try:
        doc = yaml.safe_load(PERMISSIONS_FILE.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        errors.append(f"permissions.yaml: YAML 파싱 실패 — {e}")
        return

    principals = (doc or {}).get("principals")
    if not isinstance(principals, dict):
        errors.append("permissions.yaml: 최상위 'principals' 매핑이 없다")
        return

    for principal, patterns in principals.items():
        if patterns is None:
            patterns = []
        if not isinstance(patterns, list):
            errors.append(f"permissions.yaml: {principal}: 패턴 목록(list)이어야 한다")
            continue
        for pattern in patterns:
            if not isinstance(pattern, str) or not PERMISSION_PATTERN.match(pattern):
                errors.append(
                    f"permissions.yaml: {principal}: 잘못된 패턴 '{pattern}' — "
                    "정확한 타입 또는 접두 와일드카드('stream.*')만 허용"
                )
                continue
            stream = pattern.split(".", 1)[0]
            if stream not in valid_streams:
                errors.append(
                    f"permissions.yaml: {principal}: '{pattern}' 의 stream '{stream}' 은 "
                    f"envelope enum에 없다 (허용: {sorted(valid_streams)})"
                )


def main() -> int:
    # Windows 콘솔(cp949)에서도 한글/기호 출력이 깨지지 않도록
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    errors: list[str] = []
    check_samples(errors)
    check_permissions(errors)

    if errors:
        print("스키마 호환성 게이트 실패:\n", file=sys.stderr)
        for e in errors:
            print(f"  ✗ {e}", file=sys.stderr)
        print(
            "\nbreaking 변경이 필요하면 schema_version 증가 + 업캐스터 등록으로 진행하라"
            " (ADR-017 §5).",
            file=sys.stderr,
        )
        return 1

    print("스키마 호환성 게이트 통과 ✓ (샘플 재검증 + 커버리지 + permissions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
