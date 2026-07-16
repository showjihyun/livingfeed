"""감정 상태 어댑터 — lf-emotion 순수 로직에 저장(Redis)·적재 준비를 붙인다 (ADR-015).

계산은 전부 lf-emotion(결정적 순수 함수)이고, 여기는 상태의 수화/저장과
actor.emotion.shifted 적재 준비만 담당한다. 적재 자체는 tick RESOLVE에서
principal 'engine.emotion'으로 일어난다 (permissions.yaml).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from lf_emotion import EmotionState, appraise_goal, appraise_interaction, decay, describe
from redis.asyncio import Redis

from lf_actor.persona import Persona

logger = logging.getLogger("lf.actor.emotion")

PRINCIPAL = "engine.emotion"
SHIFT_TYPE = "actor.emotion.shifted"


@dataclass(frozen=True)
class PendingShift:
    """RESOLVE에서 적재될 감정 변화 — 원인 상호작용의 인과를 승계한다."""

    actor_id: str
    payload: dict[str, Any]
    #: 원인 사건 (상호작용·행동). 목표 좌절처럼 상태 유래 감정은 원인이 없다 (None)
    causation_id: str | None
    correlation_id: str | None
    #: 이 변화를 유발한 감정 인스턴스 {type, intensity, target_id} —
    #: 관계 응고(ADR-016 갱신 규칙 2)의 입력이 된다
    instance: dict[str, Any]


class EmotionAdapter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(world_id: str, actor_id: str) -> str:
        return f"lf:emo:{world_id}:{actor_id}"

    async def load(self, world_id: str, actor_id: str) -> EmotionState:
        raw = await self._redis.get(self._key(world_id, actor_id))
        if raw is None:
            return EmotionState()
        return EmotionState.from_json(json.loads(raw))

    async def save(self, world_id: str, actor_id: str, state: EmotionState) -> None:
        await self._redis.set(
            self._key(world_id, actor_id),
            json.dumps(state.to_json(), ensure_ascii=False),
        )

    async def appraise(
        self,
        world_id: str,
        persona: Persona,
        interactions: list[dict[str, Any]],
        *,
        tick: int,
    ) -> tuple[list[PendingShift], str]:
        """상호작용들을 평가해 상태를 갱신하고, 임계 이상 변화의 적재물을 돌려준다.

        반환: (pending shifts, 현재 감정의 자연어 요약 — Working Memory 주입용).
        """
        state = await self.load(world_id, persona.id)
        shifts: list[PendingShift] = []
        for interaction in interactions:
            result = appraise_interaction(state, interaction, persona.big_five)
            state = result.state
            if result.significant:
                # 이 상호작용이 만든/강화한 인스턴스 — 대상 기준 최신 상태에서 찾는다
                triggered = next(
                    (
                        e.to_json()
                        for e in state.emotions
                        if e.source_event == interaction["event_id"]
                        or e.target_id == interaction["payload"].get("player_id")
                    ),
                    {"type": "unknown", "intensity": 0.0, "target_id": None},
                )
                shifts.append(
                    PendingShift(
                        actor_id=persona.id,
                        payload={
                            "mood": state.mood.to_json(),
                            "emotions": [e.to_json() for e in state.top_emotions()],
                            "reason": result.reason[:200],
                        },
                        causation_id=interaction["event_id"],
                        correlation_id=interaction["correlation_id"],
                        instance=triggered,
                    )
                )
        await self.save(world_id, persona.id, state)
        if shifts:
            logger.info(
                "감정 변화: %s tick=%d %d건 — %s",
                persona.id, tick, len(shifts), describe(state),
            )
        return shifts, describe(state)

    async def appraise_goal_signals(
        self,
        world_id: str,
        persona: Persona,
        signals: list[tuple[str, float, str | None, str | None, str | None]],
    ) -> list[PendingShift]:
        """목표 결과 신호들을 감정으로 (ADR-015 goal_congruence) — 진전은 기쁨, 결핍은 괴로움.

        signals: (kind, magnitude, source_event, causation_id, correlation_id) 목록.
        대상 없는 감정이라 instance.target_id는 None — 관계 응고에 스며들지 않는다.
        """
        state = await self.load(world_id, persona.id)
        shifts: list[PendingShift] = []
        for kind, magnitude, source_event, causation, correlation in signals:
            result = appraise_goal(
                state, persona.big_five, kind=kind, magnitude=magnitude, source_event=source_event
            )
            state = result.state
            if not result.significant:
                continue
            # 이번 신호가 만든/강화한 인스턴스 — 강도가 에피소드 중요도의 감정 항이
            # 된다 (ADR-008 peak). 대상은 어차피 None이라 관계엔 안 스며든다.
            triggered = next(
                (e.to_json() for e in state.emotions if e.source_event == source_event),
                {"type": "goal", "intensity": 0.0, "target_id": None},
            )
            shifts.append(
                PendingShift(
                    actor_id=persona.id,
                    payload={
                        "mood": state.mood.to_json(),
                        "emotions": [e.to_json() for e in state.top_emotions()],
                        "reason": result.reason[:200],
                    },
                    causation_id=causation,
                    correlation_id=correlation,
                    instance=triggered,
                )
            )
        await self.save(world_id, persona.id, state)
        if shifts:
            logger.info("목표 감정: %s %d건 — %s", persona.id, len(shifts), describe(state))
        return shifts

    async def decay_ticks(self, world_id: str, persona: Persona, ticks: int = 1) -> None:
        """tick 감쇠 — 인스턴스 지수 감쇠 + mood baseline 회귀 (ADR-015).

        ticks>1은 Cold 배치 경로 (ADR-012) — 인스턴스는 지수 합성이라 1-tick 반복과
        등가, mood 회귀는 엔진의 n-tick 선형 근사를 따른다 (통계적 일괄 처리).
        """
        state = await self.load(world_id, persona.id)
        if state == EmotionState():
            return  # 중립 상태는 저장할 것도 감쇠할 것도 없다
        await self.save(world_id, persona.id, decay(state, persona.big_five, ticks=ticks))
