"""감정 상태 모델 — 2층 구조 (ADR-015 §상태 표현).

mood: PAD 3차원, 느리게 변하고 성격 baseline으로 회귀한다.
emotions: 대상(target)과 출처(source_event)가 있는 활성 인스턴스 — "화남"이 아니라
"누구 때문에 화남". 이것이 관계(ADR-016)·기억 중요도(ADR-008)와의 연결 고리다.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return min(hi, max(lo, value))


@dataclass(frozen=True)
class Pad:
    pleasure: float = 0.0
    arousal: float = 0.0
    dominance: float = 0.0

    def clamped(self) -> Pad:
        return Pad(_clamp(self.pleasure), _clamp(self.arousal), _clamp(self.dominance))

    def l1_distance(self, other: Pad) -> float:
        return (
            abs(self.pleasure - other.pleasure)
            + abs(self.arousal - other.arousal)
            + abs(self.dominance - other.dominance)
        )

    def to_json(self) -> dict[str, float]:
        return {
            "pleasure": round(self.pleasure, 4),
            "arousal": round(self.arousal, 4),
            "dominance": round(self.dominance, 4),
        }


@dataclass(frozen=True)
class EmotionInstance:
    type: str
    intensity: float
    target_id: str | None
    source_event: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "intensity": round(self.intensity, 4),
            "target_id": self.target_id,
        }


@dataclass(frozen=True)
class EmotionState:
    mood: Pad = field(default_factory=Pad)
    emotions: tuple[EmotionInstance, ...] = ()

    def top_emotions(self, limit: int = 8) -> list[EmotionInstance]:
        return sorted(self.emotions, key=lambda e: -e.intensity)[:limit]

    def to_json(self) -> dict[str, Any]:
        return {
            "mood": self.mood.to_json(),
            "emotions": [
                {**e.to_json(), "source_event": e.source_event} for e in self.emotions
            ],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> EmotionState:
        mood = Pad(**{k: float(v) for k, v in data.get("mood", {}).items()})
        emotions = tuple(
            EmotionInstance(
                type=e["type"],
                intensity=float(e["intensity"]),
                target_id=e.get("target_id"),
                source_event=e.get("source_event"),
            )
            for e in data.get("emotions", [])
        )
        return cls(mood=mood, emotions=emotions)


def baseline_from_ocean(big_five: dict[str, float]) -> Pad:
    """OCEAN → baseline PAD (ADR-015 §personality 기준점).

    Mehrabian 근사 매핑의 단순화 — 계수는 params가 아니라 정의다 (성격 축의 의미).
      P = 0.4·E + 0.4·A − 0.5·N
      A = 0.5·N + 0.3·O
      D = 0.5·E − 0.4·N + 0.2·C
    """
    o = big_five.get("openness", 0.5)
    c = big_five.get("conscientiousness", 0.5)
    e = big_five.get("extraversion", 0.5)
    a = big_five.get("agreeableness", 0.5)
    n = big_five.get("neuroticism", 0.5)
    return Pad(
        pleasure=0.4 * e + 0.4 * a - 0.5 * n,
        arousal=0.5 * n + 0.3 * o - 0.25,  # 중립 성격(전부 0.5)이 대략 0에 오도록 보정
        dominance=0.5 * e - 0.4 * n + 0.2 * c - 0.15,
    ).clamped()


def merge_instance(
    existing: tuple[EmotionInstance, ...],
    incoming: EmotionInstance,
    *,
    reinforcement: float,
    max_active: int,
) -> tuple[EmotionInstance, ...]:
    """같은 (type, target)은 강화, 아니면 추가. 상한 초과 시 최약부터 소멸 (ADR-015)."""
    merged: list[EmotionInstance] = []
    reinforced = False
    for inst in existing:
        if inst.type == incoming.type and inst.target_id == incoming.target_id:
            stronger = max(inst.intensity, incoming.intensity)
            weaker = min(inst.intensity, incoming.intensity)
            merged.append(
                replace(
                    incoming,
                    intensity=min(1.0, stronger + reinforcement * weaker),
                )
            )
            reinforced = True
        else:
            merged.append(inst)
    if not reinforced:
        merged.append(incoming)
    merged.sort(key=lambda e: -e.intensity)
    return tuple(merged[:max_active])
