"""기억 응고 — tick 종료 시 에피소드 생성 (ADR-008 §기억 수명주기).

이번 tick의 재료(개입·응답·감정·행동)를 하나의 에피소드로 접는다.
요약은 템플릿 — LLM 요약은 중요도 상위 전용 후속 (ADR-008 규칙 2).
중요도 계수는 설정값이며 리플레이로 오프라인 튜닝한다 (ADR-008 규칙 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def describe_interaction(envelope: dict[str, Any]) -> str:
    """상호작용/사건 봉투 → 지각 문장 (Working Memory와 에피소드가 공유)."""
    p = envelope["payload"]
    kind = envelope["type"]
    if kind == "player.dm.sent":
        return f"플레이어 {p['player_id']}의 DM: \"{p['text']}\""
    if kind == "player.comment.posted":
        return f"플레이어 {p['player_id']}가 내 글에 댓글을 남겼다: \"{p['text']}\""
    if kind == "player.reaction.added":
        return f"플레이어 {p['player_id']}가 내 글에 좋아요를 눌렀다"
    if kind == "world.incident.occurred":
        return f"세계 사건: {p['description']}"
    return f"플레이어 상호작용: {kind}"


@dataclass(frozen=True)
class ImportanceWeights:
    """importance = w·(감정, 관계, 목표, 희소성) — ADR-008 규칙 1."""

    emotion: float = 0.35
    relationship: float = 0.30
    goal: float = 0.20
    rarity: float = 0.15
    #: 이 값 이상이면 Semantic(Qdrant)에도 임베딩된다 (ADR-008 도식)
    semantic_gate: float = 0.4


@dataclass(frozen=True)
class TickMaterials:
    """이번 tick에 액터가 겪은 일 — 응고의 입력."""

    interactions: list[dict[str, Any]] = field(default_factory=list)
    replies: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    action_envelope: dict[str, Any] | None = None
    emotion_peak: float = 0.0
    relationship_events: int = 0

    def is_empty(self) -> bool:
        return not (self.interactions or self.replies or self.action_envelope)


@dataclass(frozen=True)
class Episode:
    summary: str
    importance: float
    factors: dict[str, float]
    source_event_ids: list[str]
    tags: list[str]
    causation_id: str | None
    correlation_id: str | None


def build_episode(
    materials: TickMaterials, *, weights: ImportanceWeights | None = None
) -> Episode | None:
    """재료 → 에피소드. 아무 일 없던 tick은 기억되지 않는다 (None)."""
    weights = weights or ImportanceWeights()
    if materials.is_empty():
        return None

    lines: list[str] = []
    source_ids: list[str] = []
    tags: set[str] = set()

    for envelope in materials.interactions:
        lines.append(describe_interaction(envelope))
        source_ids.append(envelope["event_id"])
        tags.add(envelope["type"].split(".")[1])  # dm/comment/reaction/incident
    for _envelope, text in materials.replies:
        lines.append(f'나는 답했다 — "{text}"')
    if materials.action_envelope is not None:
        payload = materials.action_envelope["payload"]
        lines.append(f"나는 {payload['action_kind']} — {payload['intent']}")
        source_ids.append(materials.action_envelope["event_id"])
        tags.add(payload["action_kind"])

    # 목표 관련성은 Goal 상태가 이벤트화되는 단계의 후속이다 — 0으로 정직하게
    factors = {
        "emotion": round(min(1.0, materials.emotion_peak), 4),
        "relationship": round(min(1.0, materials.relationship_events / 2), 4),
        "goal": 0.0,
        "rarity": round(
            1.0
            if any(e["type"] == "world.incident.occurred" for e in materials.interactions)
            else (0.3 if materials.interactions else 0.1),
            4,
        ),
    }
    importance = round(
        min(
            1.0,
            weights.emotion * factors["emotion"]
            + weights.relationship * factors["relationship"]
            + weights.goal * factors["goal"]
            + weights.rarity * factors["rarity"],
        ),
        4,
    )

    first = materials.interactions[0] if materials.interactions else None
    return Episode(
        summary=" / ".join(lines)[:500],
        importance=importance,
        factors=factors,
        source_event_ids=source_ids,
        tags=sorted(tags),
        causation_id=first["event_id"] if first else None,
        correlation_id=first["correlation_id"] if first else None,
    )
