"""reflection — 경험이 신념이 되는 주기 단계 (ADR-008 §기억 수명주기).

감정·관계 '상태'의 패턴을 읽어 신념 명제를 만든다. MVP는 규칙 기반
(결정적 — 리플레이 재현). "이 경험들이 의미하는 것"을 묻는 LLM reflection은
후속이며, 신념 이벤트·Semantic 저장 계약은 그대로 간다.

신념은 (kind, about) 단위로 갱신된다: 확신이 임계 이상 변할 때만 재발행 —
사람은 매시간 같은 결론을 새로 내리지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from lf_emotion import EmotionState
from lf_relationship import RelationshipState
from redis.asyncio import Redis

#: 재발행 임계 — 직전 발행 대비 확신 변화가 이만큼 쌓여야 신념이 갱신된다
RESTATE_DELTA = 0.15


@dataclass(frozen=True)
class Belief:
    statement: str
    kind: str
    confidence: float
    about_id: str | None
    source_event_ids: list[str]


def _display(name_map: dict[str, str], target_id: str) -> str:
    return name_map.get(target_id, target_id)


def derive_beliefs(
    emotion: EmotionState,
    edges: dict[str, RelationshipState],
    *,
    name_map: dict[str, str] | None = None,
) -> list[Belief]:
    """상태 → 신념 후보 (순수·결정적). 후보 규칙이 곧 MVP 세계관이다:

    - supporter: 신뢰·친밀이 함께 자란 상대
    - threat:    원한이 쌓인 상대 (신뢰 회복과 독립 — ADR-016)
    - felt_support: 강하게 남은 감사 (감정 인스턴스 유래 — 근거 사건 추적 가능)
    """
    names = name_map or {}
    beliefs: list[Belief] = []

    for other_id in sorted(edges):
        state = edges[other_id]
        dims = state.dimensions
        if dims["trust"] >= 0.25 and dims["intimacy"] >= 0.15:
            confidence = round(min(1.0, 0.5 * dims["trust"] + 0.5 * dims["intimacy"]), 4)
            beliefs.append(
                Belief(
                    statement=(
                        f"{_display(names, other_id)}은(는) 내게 힘이 되는 사람이다 — "
                        "어려울 때 먼저 다가와줬다."
                    )[:300],
                    kind="supporter",
                    confidence=confidence,
                    about_id=other_id,
                    source_event_ids=[],
                )
            )
        if dims["resentment"] >= 0.3:
            beliefs.append(
                Belief(
                    statement=(
                        f"{_display(names, other_id)}은(는) 조심해야 할 상대다 — "
                        "쌓인 것이 쉽게 사라지지 않는다."
                    )[:300],
                    kind="threat",
                    confidence=round(min(1.0, dims["resentment"]), 4),
                    about_id=other_id,
                    source_event_ids=[],
                )
            )

    for instance in emotion.top_emotions(4):
        if instance.type == "gratitude" and instance.intensity >= 0.7 and instance.target_id:
            beliefs.append(
                Belief(
                    statement=(
                        f"{_display(names, instance.target_id)}의 지지는 진심이다 — "
                        "그 말이 오래 남아 있다."
                    )[:300],
                    kind="felt_support",
                    confidence=round(instance.intensity, 4),
                    about_id=instance.target_id,
                    source_event_ids=(
                        [instance.source_event] if instance.source_event else []
                    ),
                )
            )
    return beliefs


class BeliefLedger:
    """발행된 신념의 확신 대장 (Redis) — 임계 이상 변화만 재발행하게 한다."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(world_id: str, actor_id: str) -> str:
        return f"lf:belief:{world_id}:{actor_id}"

    async def changed(self, world_id: str, actor_id: str, belief: Belief) -> bool:
        field = f"{belief.kind}|{belief.about_id or '-'}"
        raw = await self._redis.hget(self._key(world_id, actor_id), field)
        if raw is None:
            return True
        previous = float(json.loads(raw)["confidence"])
        return abs(belief.confidence - previous) >= RESTATE_DELTA

    async def record(self, world_id: str, actor_id: str, belief: Belief) -> None:
        field = f"{belief.kind}|{belief.about_id or '-'}"
        await self._redis.hset(
            self._key(world_id, actor_id), field,
            json.dumps({"confidence": belief.confidence}, ensure_ascii=False),
        )


def belief_point_key(actor_id: str, belief: Belief) -> str:
    """Semantic 포인트의 결정적 키 — 같은 (kind, about) 신념은 덮어써 갱신된다."""
    return f"belief:{actor_id}:{belief.kind}:{belief.about_id or '-'}"
