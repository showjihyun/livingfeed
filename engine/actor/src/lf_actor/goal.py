"""목표·욕구 어댑터 — lf-goal 순수 로직에 저장(Redis)·적재 준비를 붙인다 (ADR-012).

계산은 전부 lf-goal(결정적 순수 함수)이고, 여기는 상태의 수화/저장과
actor.goal.advanced 적재 준비, decide 컨텍스트용 요약만 담당한다. 적재는
tick CONSOLIDATE에서 principal 'engine.goal'으로 일어난다 (permissions.yaml).

상태는 페르소나 목표에서 시드된다 — 첫 tick에 없으면 initial_state로 만든다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from lf_goal import (
    GoalAdvance,
    GoalState,
    appraise_action,
    decay,
    describe,
    initial_state,
    satisfy_from_interaction,
    starvation,
)
from redis.asyncio import Redis

from lf_actor.persona import Persona

logger = logging.getLogger("lf.actor.goal")

PRINCIPAL = "engine.goal"
ADVANCED_TYPE = "actor.goal.advanced"


@dataclass(frozen=True)
class PendingGoalEvent:
    """CONSOLIDATE에서 적재될 목표 진행 이벤트 — 원인 행동의 인과를 승계한다."""

    actor_id: str
    payload: dict[str, Any]
    causation_id: str | None
    correlation_id: str | None


class GoalAdapter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(world_id: str, actor_id: str) -> str:
        return f"lf:goal:{world_id}:{actor_id}"

    async def load(self, world_id: str, persona: Persona) -> GoalState:
        raw = await self._redis.get(self._key(world_id, persona.id))
        if raw is None:
            return initial_state(list(persona.goals), persona.needs_bias)
        return GoalState.from_json(json.loads(raw))

    async def save(self, world_id: str, actor_id: str, state: GoalState) -> None:
        await self._redis.set(
            self._key(world_id, actor_id),
            json.dumps(state.to_json(), ensure_ascii=False),
        )

    async def summary(self, world_id: str, persona: Persona) -> str:
        """욕구·목표 요약 한 줄 — decide 전 Working Memory 주입용 (액터가 목표를 좇게)."""
        state = await self.load(world_id, persona)
        return describe(state, list(persona.goals), persona.needs_bias)

    async def record_interaction(
        self, world_id: str, persona: Persona, interaction_type: str
    ) -> None:
        """플레이어 상호작용이 욕구를 채운다 (PERCEIVE). 목표 진행·이벤트는 없다."""
        state = await self.load(world_id, persona)
        after = satisfy_from_interaction(state, interaction_type)
        if after != state:
            await self.save(world_id, persona.id, after)

    async def record_action(
        self, world_id: str, persona: Persona, action_envelope: dict[str, Any]
    ) -> tuple[float, list[PendingGoalEvent]]:
        """확정 행동을 평가한다 (CONSOLIDATE) — 욕구 충족 + 목표 진행.

        반환: (congruence, 임계 넘긴 목표 진행 이벤트들). congruence는 이번 tick
        에피소드의 goal 중요도 항이 된다 (ADR-008 §중요도 0.20).
        """
        payload = action_envelope["payload"]
        state = await self.load(world_id, persona)
        result = appraise_action(state, payload["action_kind"], list(persona.goals), persona.needs_bias)
        await self.save(world_id, persona.id, result.state)
        events = [self._advance_event(persona.id, action_envelope, adv) for adv in result.advances]
        if events:
            logger.info(
                "목표 진행: %s %s congruence=%.2f",
                persona.id, [a.goal_id for a in result.advances], result.congruence,
            )
        return result.congruence, events

    def _advance_event(
        self, actor_id: str, cause: dict[str, Any], advance: GoalAdvance
    ) -> PendingGoalEvent:
        return PendingGoalEvent(
            actor_id=actor_id,
            payload={
                "goal_id": advance.goal_id,
                "description": advance.description,
                "progress": advance.progress,
                "need": advance.need,
                "congruence": advance.congruence,
            },
            causation_id=cause["event_id"],
            correlation_id=cause.get("correlation_id"),
        )

    async def starvation_signal(
        self, world_id: str, persona: Persona
    ) -> tuple[str, float] | None:
        """가장 목마른 욕구가 결핍이면 (need, 결핍도) — 좌절 감정의 입력 (ADR-015)."""
        state = await self.load(world_id, persona)
        return starvation(state, persona.needs_bias)

    async def decay_one_tick(self, world_id: str, persona: Persona) -> None:
        """tick 감쇠 — 욕구 만족도가 되돌아온다 (ADR-012). 목표 진행은 남는다."""
        state = await self.load(world_id, persona)
        decayed = decay(state, 1)
        if decayed != state:
            await self.save(world_id, persona.id, decayed)
