"""Goal 상태 모델 — 욕구 게이지 + 목표 진행 (ADR-012 §인지 루프 need/goal, docs/plan/04).

needs: 욕구 만족도 게이지 0..1 (높을수록 채워짐). 시간이 지나면 감쇠하고
(욕구가 되돌아온다), 그 욕구를 채우는 행동으로 다시 오른다.
goals: 목표별 진행도 0..1. 목표는 감쇠하지 않는다 — 이룬 것은 남는다.
pending: 직전 발행 이후 누적된 미발행 진행 델타 (임계 판정 대상, 관계 pending과 동형).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 3대 욕구 축 (docs/plan/04 — needs_bias 페르소나 필드와 같은 이름)
NEEDS = ("achievement", "belonging", "security")


def clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def zero_needs() -> dict[str, float]:
    return dict.fromkeys(NEEDS, 0.0)


@dataclass(frozen=True)
class GoalState:
    needs: dict[str, float] = field(default_factory=zero_needs)
    goals: dict[str, float] = field(default_factory=dict)
    pending: dict[str, float] = field(default_factory=dict)

    def pending_max(self) -> float:
        return max(self.pending.values(), default=0.0)

    def to_json(self) -> dict[str, Any]:
        return {
            "needs": {k: round(v, 4) for k, v in self.needs.items()},
            "goals": {k: round(v, 4) for k, v in self.goals.items()},
            "pending": {k: round(v, 4) for k, v in self.pending.items()},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> GoalState:
        return cls(
            needs={n: float(data.get("needs", {}).get(n, 0.0)) for n in NEEDS},
            goals={k: float(v) for k, v in data.get("goals", {}).items()},
            pending={k: float(v) for k, v in data.get("pending", {}).items()},
        )
