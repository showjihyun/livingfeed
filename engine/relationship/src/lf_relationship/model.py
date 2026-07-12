"""관계 상태 모델 — 방향성 5차원 + stage + salience (ADR-016 §관계 상태).

pending은 직전 발행 이후 누적된 미발행 델타다 — 임계 판정의 대상이며,
발행 시점에 비워진다 (스팸도 침묵도 없는 발행 규칙의 상태).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DIMENSIONS = ("trust", "intimacy", "respect", "attraction", "resentment")

#: 차원별 값 범위 — trust/respect는 양극, 나머지는 단극 (ADR-016)
_BOUNDS = {
    "trust": (-1.0, 1.0),
    "intimacy": (0.0, 1.0),
    "respect": (-1.0, 1.0),
    "attraction": (0.0, 1.0),
    "resentment": (0.0, 1.0),
}

STAGES = (
    "stranger", "acquaintance", "friend", "close_friend",
    "romantic", "family", "mentor", "rival", "enemy",
)


def clamp_dimension(name: str, value: float) -> float:
    lo, hi = _BOUNDS[name]
    return min(hi, max(lo, value))


def zero_dimensions() -> dict[str, float]:
    return dict.fromkeys(DIMENSIONS, 0.0)


@dataclass(frozen=True)
class RelationshipState:
    dimensions: dict[str, float] = field(default_factory=zero_dimensions)
    stage: str = "stranger"
    salience: float = 0.0
    #: 직전 발행 이후 누적 미발행 델타 (salience 포함하지 않음 — 차원만 임계 대상)
    pending: dict[str, float] = field(default_factory=zero_dimensions)

    def pending_l1(self) -> float:
        return sum(abs(v) for v in self.pending.values())

    def to_json(self) -> dict[str, Any]:
        return {
            "dimensions": {k: round(v, 4) for k, v in self.dimensions.items()},
            "stage": self.stage,
            "salience": round(self.salience, 4),
            "pending": {k: round(v, 4) for k, v in self.pending.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RelationshipState:
        return cls(
            dimensions={k: float(data["dimensions"].get(k, 0.0)) for k in DIMENSIONS},
            stage=data.get("stage", "stranger"),
            salience=float(data.get("salience", 0.0)),
            pending={k: float(data.get("pending", {}).get(k, 0.0)) for k in DIMENSIONS},
        )
