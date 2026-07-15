"""reflection — 경험이 신념이 되는 주기 단계 (ADR-008 §기억 수명주기).

두 경로가 한 계약을 공유한다 (신념 이벤트·Semantic 저장 동일):
- 규칙(derive_beliefs): 감정·관계 '상태'의 패턴 — 결정적, 리플레이 재현, 언제나 돈다.
- LLM(insight_*): "이 경험들이 의미하는 것" — 작업 기억을 곱씹은 통찰 하나를 더한다.
  미지원(rule 프로바이더)·실패면 조용히 생략된다 — 규칙 신념이 바닥을 지킨다.

신념은 (kind, about) 단위로 갱신된다: 확신이 임계 이상 변할 때만 재발행 —
사람은 매시간 같은 결론을 새로 내리지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from lf_emotion import EmotionState
from lf_relationship import RelationshipState
from redis.asyncio import Redis

#: 재발행 임계 — 직전 발행 대비 확신 변화가 이만큼 쌓여야 신념이 갱신된다
RESTATE_DELTA = 0.15

#: LLM 통찰의 닫힌 분류 — 규칙 신념(supporter/threat/felt_support)과 자리가 겹치지 않는다
INSIGHT_KINDS = ("self_image", "world_view", "person_insight")

#: 철회된 신념에 남는 잔불 확신 — 재확립은 RESTATE_DELTA를 다시 넘어야 한다 (히스테리시스)
RETRACTED_CONFIDENCE = 0.05

#: 규칙 신념별 철회문 — 근거 상태가 무너졌을 때 같은 자리에 재발행된다 (신념 폐기)
_RETRACTIONS = {
    "supporter": "{name}이(가) 힘이 되는 사람이라 믿었지만 — 지금은 그 확신이 흐려졌다.",
    "threat": "{name}을(를) 조심해야 한다고 여겼지만 — 그 경계가 조금씩 풀리고 있다.",
    "felt_support": "{name}의 지지가 진심이라 여겼던 마음이, 시간과 함께 옅어졌다.",
}


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


def retract_stale(
    published: dict[str, float],
    derived: list[Belief],
    *,
    name_map: dict[str, str] | None = None,
) -> list[Belief]:
    """근거 상태가 무너진 규칙 신념의 철회 (ADR-008 신념 폐기).

    대장(published: 'kind|about' → 확신)의 슬롯 중 이번 주기에 더 이상 도출되지
    않는 규칙 신념을 저확신 철회문으로 재발행한다 — 같은 (kind, about) 자리
    갱신이라 read 모델·Semantic이 그대로 덮어쓴다. 이미 잔불로 내려간 슬롯은
    대장 임계(RESTATE_DELTA)가 재발행을 막는다. LLM 통찰은 상태 도출이 아니라
    철회 대상이 아니다 — 반증은 새 통찰의 몫이다.
    """
    names = name_map or {}
    alive = {f"{b.kind}|{b.about_id or '-'}" for b in derived}
    retractions: list[Belief] = []
    for slot in sorted(published):
        kind, _, about = slot.partition("|")
        if kind not in _RETRACTIONS or slot in alive:
            continue
        about_id = None if about == "-" else about
        retractions.append(
            Belief(
                statement=_RETRACTIONS[kind].format(
                    name=_display(names, about_id) if about_id else "그 사람"
                )[:300],
                kind=kind,
                confidence=RETRACTED_CONFIDENCE,
                about_id=about_id,
                source_event_ids=[],
            )
        )
    return retractions


def insight_schema(known_ids: list[str]) -> dict[str, Any]:
    """LLM reflection 응답 스키마 — 통찰 하나. about은 아는 사람 안에서만 (닫힌 어휘)."""
    return {
        "type": "object",
        "properties": {
            "statement": {"type": "string", "minLength": 1, "maxLength": 300},
            "kind": {"type": "string", "enum": list(INSIGHT_KINDS)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "about_actor_id": {"enum": [*known_ids, None]},
        },
        "required": ["statement", "kind", "confidence", "about_actor_id"],
        "additionalProperties": False,
    }


def insight_to_belief(output: dict[str, Any], known_ids: set[str]) -> Belief | None:
    """검증된 LLM 통찰 → Belief. 하드룰 재집행 — 무효면 None (신념은 조용히 생략).

    환각 대상은 끊는다(ADR-014 §대상 폴리시): 아는 사람 밖 about은 null로,
    대상 없는 인물 통찰은 통찰이 아니다. 확신은 [0,1] 클램프.
    """
    statement = str(output.get("statement") or "").strip()[:300]
    kind = output.get("kind")
    if not statement or kind not in INSIGHT_KINDS:
        return None
    about = output.get("about_actor_id")
    if about is not None and about not in known_ids:
        about = None
    if kind == "person_insight":
        if about is None:
            return None  # 인물 통찰은 대상이 있어야 한다
    else:
        about = None  # 자기/세계 통찰의 대상은 자기 자신이다 (스키마: null)
    try:
        confidence = max(0.0, min(1.0, round(float(output.get("confidence")), 4)))
    except (TypeError, ValueError):
        return None
    if confidence <= 0.0:
        return None  # 확신 없는 신념은 신념이 아니다
    return Belief(
        statement=statement, kind=kind, confidence=confidence,
        about_id=about, source_event_ids=[],
    )


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

    async def entries(self, world_id: str, actor_id: str) -> dict[str, float]:
        """발행된 슬롯 전체 ('kind|about' → 확신) — 철회 후보 탐색용 (신념 폐기)."""
        raw = await self._redis.hgetall(self._key(world_id, actor_id))
        return {
            (k.decode() if isinstance(k, bytes) else k): float(json.loads(v)["confidence"])
            for k, v in raw.items()
        }


def belief_point_key(actor_id: str, belief: Belief) -> str:
    """Semantic 포인트의 결정적 키 — 같은 (kind, about) 신념은 덮어써 갱신된다."""
    return f"belief:{actor_id}:{belief.kind}:{belief.about_id or '-'}"
