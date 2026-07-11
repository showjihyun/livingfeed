"""스키마·발행 권한 매트릭스의 런타임 접근 (ADR-017 §2).

원천은 packages/schemas/{events/*.schema.json, permissions.yaml}이고,
generate.py가 generated/schemas/ 로 복사한 패키지 데이터를 여기서 읽는다.
드리프트는 CI의 코드젠 diff 게이트가 차단한다.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files
from json import loads
from typing import Any

import yaml

_DATA = files("lf_schemas") / "generated" / "schemas"


@cache
def envelope_schema() -> dict[str, Any]:
    """공통 봉투 JSON Schema (ADR-002)."""
    return loads((_DATA / "envelope.schema.json").read_text(encoding="utf-8"))


@cache
def payload_schema(event_type: str) -> dict[str, Any]:
    """타입별 payload JSON Schema. 등록되지 않은 타입이면 KeyError."""
    resource = _DATA / f"{event_type}.schema.json"
    try:
        text = resource.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise KeyError(f"등록되지 않은 이벤트 타입: {event_type}") from None
    return loads(text)


@cache
def known_event_types() -> frozenset[str]:
    """payload 스키마가 등록된 전체 이벤트 타입."""
    return frozenset(
        f.name.removesuffix(".schema.json")
        for f in _DATA.iterdir()
        if f.name.endswith(".schema.json") and f.name != "envelope.schema.json"
    )


@cache
def permissions() -> dict[str, tuple[str, ...]]:
    """발행 권한 매트릭스: principal → 허용 패턴 목록 (ADR-017 §2)."""
    doc = yaml.safe_load((_DATA / "permissions.yaml").read_text(encoding="utf-8"))
    return {p: tuple(patterns or ()) for p, patterns in doc["principals"].items()}


def is_allowed(principal: str, event_type: str) -> bool:
    """principal이 event_type을 발행할 수 있는가.

    패턴은 정확한 타입("actor.action.performed") 또는
    접두 와일드카드("actor.*" — 'actor.' 로 시작하는 전부)만 허용된다.
    등록되지 않은 principal은 무조건 거부.
    """
    patterns = permissions().get(principal)
    if patterns is None:
        return False
    for pattern in patterns:
        if pattern.endswith(".*"):
            if event_type.startswith(pattern[:-1]):
                return True
        elif event_type == pattern:
            return True
    return False
