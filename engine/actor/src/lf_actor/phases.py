"""tick 파이프라인의 Actor Runtime 구현 (ADR-011/012).

인지 루프의 Phase 1 절단면:
  perceive → (appraise/emotion은 ADR-015 단계에서) → decide → act(RESOLVE 적재)
decide는 Context Fabric 조립 → AI Runtime 호출, 실패 시 규칙 폴백 —
액터는 '머뭇거린' 것으로 처리되고 tick은 멈추지 않는다.
"""

from __future__ import annotations

import logging
from typing import Any

from lf_eventstore import NewEvent, append, current_head
from lf_schemas import registry
from lf_tick.lod import ActorLod, Tier, due_by_tier, scheduled_counts
from lf_tick.pipeline import TickContext

from lf_actor.client import AiRuntimeClient
from lf_actor.context import WorldContext, build
from lf_actor.memory import WorkingMemory
from lf_actor.persona import Persona
from lf_actor.rules import fallback_action

logger = logging.getLogger("lf.actor.phases")

PRINCIPAL = "engine.actor"
ACTION_TYPE = "actor.action.performed"


class ActorPhases:
    """등록된 페르소나들을 tick 파이프라인에 태운다.

    Phase 1: 워커 in-process 실행 (단일 세계, 소수 액터). 샤드 워커 분리와
    메일박스(상호작용 경로)는 ADR-012의 다음 증분이다.
    """

    def __init__(
        self,
        personas: list[Persona],
        *,
        ai: AiRuntimeClient,
        memory: WorkingMemory,
    ) -> None:
        if not personas:
            raise ValueError("액터가 없다 — 최소 1명의 페르소나가 필요하다")
        self._personas = {p.id: p for p in personas}
        self._ai = ai
        self._memory = memory
        # 첫 액터들은 세계의 주인공 — Hot으로 시작 (승격/강등은 관심 신호 소스가 생기면)
        self._lods: dict[str, ActorLod] = {
            actor_id: ActorLod(tier=Tier.HOT, last_interest_tick=0) for actor_id in self._personas
        }
        self._intents: list[tuple[str, str, dict[str, Any]]] = []  # (actor_id, tier, payload)

    async def schedule(self, ctx: TickContext) -> dict[str, int]:
        return scheduled_counts(self._lods, ctx.tick)

    async def world(self, ctx: TickContext) -> None:
        return None  # 환경 이벤트/Director 개입은 ADR-013 단계에서

    async def perceive(self, ctx: TickContext) -> None:
        # Phase 1: 지각 소스(타 액터 관측·세계 이벤트 구독)가 아직 없다.
        # 자기 행동의 기억은 resolve에서 Working Memory로 유입된다 (ADR-008).
        return None

    async def decide(self, ctx: TickContext) -> dict[str, int]:
        self._intents = []
        decided = {"hot": 0, "warm": 0, "cold": 0}
        due = due_by_tier(self._lods, ctx.tick)
        world = WorldContext(world_id=ctx.world_id, tick=ctx.tick, world_time=ctx.world_time)
        schema = registry.payload_schema(ACTION_TYPE)

        for tier in (Tier.HOT, Tier.WARM):
            for actor_id in due[tier]:
                persona = self._personas[actor_id]
                working = await self._memory.recent(ctx.world_id, actor_id)
                bundle = build(persona, working, world)
                payload = await self._ai.decide_action(
                    bundle, schema, tier=tier.value, actor_id=actor_id, tick=ctx.tick
                )
                if payload is None:
                    payload = fallback_action(persona, ctx.tick, bundle.trace_id)
                self._intents.append((actor_id, tier.value, payload))
                decided[tier.value] += 1

        # Cold 티어는 ColdSimulator(통계 일괄 처리)의 몫 — Phase 1은 대상 없음 (ADR-012)
        return decided

    async def resolve(self, ctx: TickContext) -> int:
        """충돌 해소 → 확정 행동 적재. actor_id 순 순차·결정적 (ADR-011 §4)."""
        emitted = 0
        for actor_id, _tier, payload in sorted(self._intents, key=lambda item: item[0]):
            head = await current_head(ctx.conn, ctx.world_id, "actor", actor_id)
            [stored] = await append(
                ctx.conn,
                PRINCIPAL,
                [
                    NewEvent(
                        world_id=ctx.world_id,
                        stream="actor",
                        stream_key=actor_id,
                        type=ACTION_TYPE,
                        tick=ctx.tick,
                        actor_id=actor_id,
                        payload=payload,
                    )
                ],
                expected_head=head,
            )
            emitted += 1
            # 자기 행동 → Working Memory 유입 (지각의 최소 형태, ADR-008)
            await self._memory.add(
                ctx.world_id,
                actor_id,
                f"tick {ctx.tick}: 나는 {payload['action_kind']} — {payload['intent']}",
            )
            logger.info(
                "행동 확정: %s tick=%d kind=%s seq=%d",
                actor_id, ctx.tick, payload["action_kind"], stored.stream_seq,
            )
        self._intents = []
        return emitted

    async def consolidate(self, ctx: TickContext) -> None:
        return None  # 기억 응고·감쇠는 ADR-008/015 단계에서
