"""페르소나 로더 — agents/personas/*.yaml 이 원천이다 (ADR-001/012).

identity는 불변이다. 상태(감정·욕구·목표 진행)는 이벤트에서 파생되며
여기 실리지 않는다 (ADR-002 규칙 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    archetype: str
    identity_core: str
    big_five: dict[str, float] = field(default_factory=dict)
    needs_bias: dict[str, float] = field(default_factory=dict)
    goals: tuple[dict[str, Any], ...] = ()
    secrets: tuple[dict[str, Any], ...] = ()


def load_persona(path: Path) -> Persona:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Persona(
        id=doc["id"],
        name=doc["name"],
        archetype=doc.get("archetype", ""),
        identity_core=doc.get("identity_core", "").strip(),
        big_five=dict(doc.get("big_five") or {}),
        needs_bias=dict(doc.get("needs_bias") or {}),
        goals=tuple(doc.get("goals") or ()),
        secrets=tuple(doc.get("secrets") or ()),
    )


def load_personas(directory: Path) -> list[Persona]:
    """디렉터리의 전체 페르소나 (파일명 순 — 결정적)."""
    return [load_persona(p) for p in sorted(directory.glob("*.yaml"))]
