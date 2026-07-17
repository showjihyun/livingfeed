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
    #: 생활 패턴 — SNS 포스팅 리듬의 키 (rhythm.py). 미지의 값은 사용처에서
    #: flexible로 폴백한다 (전방 호환 — consolidation.action_label 선례)
    lifestyle: str = "flexible"
    #: 휴면 스위치 — False면 세계에 실리지 않는다 (load_personas가 건너뛴다).
    #: 파일·역사·관계는 남는다 — 삭제가 아니라 잠듦이다 (페르소나 스튜디오)
    active: bool = True
    #: 이 인물을 빚은 플레이어(^p_) — 스튜디오 태생의 저자성. 시스템 태생은 None
    created_by: str | None = None


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
        lifestyle=doc.get("lifestyle") or "flexible",
        active=bool(doc.get("active", True)),
        created_by=doc.get("created_by"),
    )


def load_personas(directory: Path) -> list[Persona]:
    """디렉터리의 깨어 있는 페르소나 (파일명 순 — 결정적). 휴면(active=false)은 건너뛴다."""
    return [p for p in (load_persona(f) for f in sorted(directory.glob("*.yaml"))) if p.active]
