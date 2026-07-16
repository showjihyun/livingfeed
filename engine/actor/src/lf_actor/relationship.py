"""관계 상태 어댑터 — lf-relationship 순수 로직에 저장(Redis)·적재 준비를 붙인다 (ADR-016).

계산은 전부 lf-relationship(결정적 순수 함수)이고, 여기는 엣지 상태의
수화/저장, sparse 인덱스(액터당 상한), relationship.* 적재 준비만 담당한다.
적재는 tick CONSOLIDATE에서 principal 'engine.relationship'으로 일어난다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from lf_relationship import (
    RelationshipState,
    UpdateResult,
    apply_interaction,
    consolidate_emotion,
    consolidate_insight,
    consume_pending,
    decay,
    default_params,
    describe_edges,
    stage_after_action,
    transition_stage,
)
from redis.asyncio import Redis

logger = logging.getLogger("lf.actor.relationship")

PRINCIPAL = "engine.relationship"
CHANGED_TYPE = "relationship.state.changed"
MILESTONE_TYPE = "relationship.milestone.reached"


@dataclass(frozen=True)
class PendingRelEvent:
    """CONSOLIDATE에서 적재될 관계 이벤트 — stream_key는 'from|to' (방향별 스트림)."""

    from_id: str
    to_id: str
    type: str
    payload: dict[str, Any]
    causation_id: str | None
    correlation_id: str | None


class RelationshipAdapter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._params = default_params()

    @staticmethod
    def _state_key(world_id: str, from_id: str, to_id: str) -> str:
        return f"lf:rel:{world_id}:{from_id}:{to_id}"

    @staticmethod
    def _index_key(world_id: str, from_id: str) -> str:
        return f"lf:relidx:{world_id}:{from_id}"

    async def load(self, world_id: str, from_id: str, to_id: str) -> RelationshipState | None:
        raw = await self._redis.get(self._state_key(world_id, from_id, to_id))
        return None if raw is None else RelationshipState.from_json(json.loads(raw))

    async def save(
        self, world_id: str, from_id: str, to_id: str, state: RelationshipState
    ) -> None:
        await self._redis.set(
            self._state_key(world_id, from_id, to_id),
            json.dumps(state.to_json(), ensure_ascii=False),
        )

    async def counterparts(self, world_id: str, from_id: str) -> list[str]:
        members = await self._redis.smembers(self._index_key(world_id, from_id))
        return sorted(m.decode() if isinstance(m, bytes) else m for m in members)

    async def summary(
        self, world_id: str, from_id: str, names: dict[str, str] | None = None
    ) -> str | None:
        """decide 컨텍스트용 관계 요약 — Relationship(3) 섹션 재료 (ADR-009 §3).

        엣지 전체를 수화해 describe_edges(순수)로 접는다. 액터당 엣지 수는
        max_active_edges가 상한이라 tick당 조회는 유계다. 엣지 없으면 None.
        """
        edges: dict[str, RelationshipState] = {}
        for to_id in await self.counterparts(world_id, from_id):
            state = await self.load(world_id, from_id, to_id)
            if state is not None:
                edges[to_id] = state
        return describe_edges(edges, names)

    async def _ensure_edge(
        self, world_id: str, from_id: str, to_id: str, cause: dict[str, Any]
    ) -> tuple[RelationshipState, list[PendingRelEvent]]:
        """엣지 수화 — 없으면 생성(sparse)하고 first_met 마일스톤을 만든다 (ADR-016)."""
        state = await self.load(world_id, from_id, to_id)
        if state is not None:
            return state, []

        state = transition_stage(RelationshipState(), "acquaintance")
        await self._register_edge(world_id, from_id, to_id)
        milestone = PendingRelEvent(
            from_id=from_id,
            to_id=to_id,
            type=MILESTONE_TYPE,
            payload={
                "from_id": from_id,
                "to_id": to_id,
                "milestone": "first_met",
                "stage": state.stage,
                "note": f"{cause.get('type', '상호작용')}(으)로 처음 연결됐다"[:200],
            },
            causation_id=cause.get("event_id"),
            correlation_id=cause.get("correlation_id"),
        )
        return state, [milestone]

    async def _register_edge(self, world_id: str, from_id: str, to_id: str) -> None:
        index = self._index_key(world_id, from_id)
        await self._redis.sadd(index, to_id)
        # 활성 상한(Dunbar) — 초과 시 최저 salience부터 휴면화 (ADR-016 리스크)
        size = await self._redis.scard(index)
        if size <= self._params["max_active_edges"]:
            return
        weakest_id, weakest_salience = None, float("inf")
        for candidate in await self.counterparts(world_id, from_id):
            if candidate == to_id:
                continue
            state = await self.load(world_id, from_id, candidate)
            salience = state.salience if state else 0.0
            if salience < weakest_salience:
                weakest_id, weakest_salience = candidate, salience
        if weakest_id is not None:
            await self._redis.srem(index, weakest_id)
            await self._redis.delete(self._state_key(world_id, from_id, weakest_id))
            logger.info("관계 휴면화: %s→%s (salience=%.3f)", from_id, weakest_id, weakest_salience)

    def _changed_event(
        self,
        from_id: str,
        to_id: str,
        state: RelationshipState,
        deltas: dict[str, float],
        reason: str,
        cause: dict[str, Any],
    ) -> PendingRelEvent:
        return PendingRelEvent(
            from_id=from_id,
            to_id=to_id,
            type=CHANGED_TYPE,
            payload={
                "from_id": from_id,
                "to_id": to_id,
                "dimensions": {k: round(v, 4) for k, v in state.dimensions.items()},
                "deltas": deltas,
                "stage": state.stage,
                "salience": round(state.salience, 4),
                "reason": reason[:200],
            },
            causation_id=cause.get("event_id"),
            correlation_id=cause.get("correlation_id"),
        )

    async def _apply(
        self,
        world_id: str,
        from_id: str,
        to_id: str,
        result: UpdateResult,
        cause: dict[str, Any],
        created: list[PendingRelEvent],
    ) -> list[PendingRelEvent]:
        events = list(created)
        state = result.state
        if result.publish:
            state, deltas = consume_pending(state)
            events.append(
                self._changed_event(from_id, to_id, state, deltas, result.reason, cause)
            )
        await self.save(world_id, from_id, to_id, state)
        return events

    async def record_interaction(
        self,
        world_id: str,
        from_id: str,
        to_id: str,
        source_kind: str,
        direction: str,
        cause: dict[str, Any],
    ) -> list[PendingRelEvent]:
        """상호작용 효과 (갱신 규칙 1) — from→to 엣지에 방향별 델타."""
        state, created = await self._ensure_edge(world_id, from_id, to_id, cause)
        result = apply_interaction(state, source_kind, direction, params=self._params)
        return await self._apply(world_id, from_id, to_id, result, cause, created)

    async def record_stage_action(
        self,
        world_id: str,
        actor_id: str,
        target_id: str,
        action_kind: str,
        cause: dict[str, Any],
    ) -> list[PendingRelEvent]:
        """상위 전이 행동(confess/sever) → stage 전이 저장 + 마일스톤 적재 준비 (ADR-016).

        판정은 순수 규칙(stage_after_action)이고, 여기는 양쪽 엣지 수화(없으면
        생성 — 모르는 사람에게 고백할 수도 있다)·저장과 마일스톤 1건(행위자
        방향 엣지 기준)만 담당한다. 거절도 마일스톤이다 — 세계에 남는 사건.
        전이 판정이 None(재고백·재절교)이면 마일스톤 없이 조용히 끝난다.
        """
        actor_edge, created = await self._ensure_edge(world_id, actor_id, target_id, cause)
        target_edge, reverse_created = await self._ensure_edge(world_id, target_id, actor_id, cause)
        events = created + reverse_created
        transition = stage_after_action(action_kind, actor_edge, target_edge)
        if transition is not None:
            if transition.actor_stage is not None:
                actor_edge = transition_stage(actor_edge, transition.actor_stage)
            if transition.target_stage is not None:
                target_edge = transition_stage(target_edge, transition.target_stage)
            events.append(
                PendingRelEvent(
                    from_id=actor_id,
                    to_id=target_id,
                    type=MILESTONE_TYPE,
                    payload={
                        "from_id": actor_id,
                        "to_id": target_id,
                        "milestone": transition.milestone,
                        "stage": actor_edge.stage,  # 전이 후 stage (전이 없으면 현 stage)
                        "note": transition.note[:200],
                    },
                    causation_id=cause.get("event_id"),
                    correlation_id=cause.get("correlation_id"),
                )
            )
        await self.save(world_id, actor_id, target_id, actor_edge)
        await self.save(world_id, target_id, actor_id, target_edge)
        return events

    async def record_emotion(
        self,
        world_id: str,
        from_id: str,
        to_id: str,
        emotion_type: str,
        intensity: float,
        cause: dict[str, Any],
    ) -> list[PendingRelEvent]:
        """감정 응고 (갱신 규칙 2) — 대상 있는 감정이 관계로 스며든다."""
        state, created = await self._ensure_edge(world_id, from_id, to_id, cause)
        result = consolidate_emotion(state, emotion_type, intensity, params=self._params)
        return await self._apply(world_id, from_id, to_id, result, cause, created)

    async def record_insight(
        self, world_id: str, from_id: str, to_id: str, confidence: float
    ) -> None:
        """인물 통찰(person_insight)이 관계 비중에 스민다 (ADR-008 → ADR-016).

        엣지가 없으면 만들지 않는다 — 생각만으로 관계가 시작되진 않는다.
        발행 대상도 아니다 (조용한 내면 변화) — 상태 저장뿐이다.
        """
        state = await self.load(world_id, from_id, to_id)
        if state is None:
            return
        result = consolidate_insight(state, confidence, params=self._params)
        await self.save(world_id, from_id, to_id, result.state)

    async def decay_all(
        self, world_id: str, from_id: str, *, ticks: int = 1
    ) -> list[PendingRelEvent]:
        """시간 감쇠 (갱신 규칙 4) — 식은 관계도 임계를 넘으면 세계에 기록된다.

        ticks>1은 Cold 배치 경로 (ADR-012) — 선형 감쇠·pending 누적이라 등가 합성.
        """
        events: list[PendingRelEvent] = []
        for to_id in await self.counterparts(world_id, from_id):
            state = await self.load(world_id, from_id, to_id)
            if state is None:
                continue
            decayed = decay(state, ticks, params=self._params)
            if decayed.pending_l1() >= self._params["publish_threshold"]:
                cleared, deltas = consume_pending(decayed)
                events.append(
                    self._changed_event(
                        from_id, to_id, cleared, deltas, "시간 감쇠 — 관계가 식었다", {}
                    )
                )
                decayed = cleared
            await self.save(world_id, from_id, to_id, decayed)
        return events
