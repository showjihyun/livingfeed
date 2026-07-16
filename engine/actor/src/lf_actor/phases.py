"""tick 파이프라인의 Actor Runtime 구현 (ADR-011/012).

인지 루프의 Phase 절단면:
  perceive(메일박스 drain) → (appraise/emotion은 ADR-015 단계에서)
  → decide(행동 + 플레이어 응답) → act(RESOLVE 적재)
decide는 Context Fabric 조립 → AI Runtime 호출, 실패 시 규칙 폴백 —
액터는 '머뭇거린' 것으로 처리되고 tick은 멈추지 않는다.
플레이어 상호작용(댓글/DM)은 반드시 응답된다 (상호작용 우선, ADR-012 규칙 2).
"""

from __future__ import annotations

import logging
from typing import Any

from lf_eventstore import NewEvent, append, current_head
from lf_relationship import STAGE_ACTION_KINDS
from lf_schemas import registry
from lf_tick.lod import (
    ActorLod,
    Tier,
    due_by_tier,
    is_due,
    maybe_demote,
    promote,
    scheduled_counts,
    touch,
)
from lf_tick.pipeline import TickContext
from redis.asyncio import Redis

from lf_actor.arc import Arc, ArcStore
from lf_actor.client import AiRuntimeClient
from lf_actor.consolidation import (
    Episode,
    ImportanceWeights,
    TickMaterials,
    build_episode,
    describe_interaction,
)
from lf_actor.context import WorldContext, build
from lf_actor.conversation import conversation_turns
from lf_actor.emotion import PRINCIPAL as EMOTION_PRINCIPAL
from lf_actor.emotion import SHIFT_TYPE, EmotionAdapter, PendingShift
from lf_actor.goal import PRINCIPAL as GOAL_PRINCIPAL
from lf_actor.goal import GoalAdapter, PendingGoalEvent
from lf_actor.ledger import DecayLedger
from lf_actor.mailbox import Mailbox
from lf_actor.memory import WorkingMemory
from lf_actor.persona import Persona
from lf_actor.reflection import (
    Belief,
    BeliefLedger,
    belief_point_key,
    derive_beliefs,
    insight_schema,
    insight_to_belief,
    retract_stale,
)
from lf_actor.relationship import PRINCIPAL as REL_PRINCIPAL
from lf_actor.relationship import PendingRelEvent, RelationshipAdapter
from lf_actor.rules import fallback_action, fallback_reply, routine_action
from lf_actor.semantic import SemanticMemory

logger = logging.getLogger("lf.actor.phases")

PRINCIPAL = "engine.actor"
ACTION_TYPE = "actor.action.performed"
MESSAGE_TYPE = "actor.message.sent"
MEMORY_TYPE = "actor.memory.consolidated"
BELIEF_TYPE = "actor.belief.formed"
IDENTITY_TYPE = "actor.identity.declared"
#: Director의 LOD 승격 신호 — 지각이 아니라 제어다 (ADR-013). perceive가 LOD만 올린다
SPOTLIGHT_TYPE = "system.director.spotlighted"
#: Director의 인생 아크 계획 — 제어 신호. perceive가 ArcStore에 저장(기억엔 안 넣는다)
ARC_TYPE = "system.director.arc_planned"

#: 프로필 소개문 상한 — identity_core를 이 길이로 자른다 (스키마 bio maxLength와 맞춘다)
_BIO_MAX = 500

#: 응답 의무가 있는 상호작용 (반응(like)은 지각·감정 입력일 뿐 응답하지 않는다)
_REPLYABLE = {"player.dm.sent": "dm", "player.comment.posted": "comment"}

#: Director의 사적 지목 (nudge_perception) — 반응을 기대하는 관측이다 (ADR-013)
OBSERVATION_TYPE = "world.observation.surfaced"
INCIDENT_TYPE = "world.incident.occurred"
#: 이 강도 이상의 세계 사건은 잠든 액터도 깨운다 — 고중요도 이벤트 승격 (ADR-011 §관심 신호)
HIGH_INTENSITY = 0.7


def sanitize_target(payload: dict[str, Any], valid_ids: set[str], actor_id: str) -> dict[str, Any]:
    """LLM이 지어낸 대상을 소스에서 끊는다 (ADR-014 §대상 폴리시).

    target_actor_id가 세계의 유효 액터가 아니거나 자기 자신이면 None으로 만든다.
    유효 액터 집합(self._personas)이 권위다. 환각 대상을 그대로 두면 피드 제목·
    participants뿐 아니라 관계 엣지·그래프 노드·drama 점수까지 유령이 번지므로,
    이벤트로 굳기 전에 끊는다 — 대상이 사라진 행동은 단독 행동이 된다(반응할
    상대가 애초에 없었으므로). 반환은 새 dict(입력 불변).
    """
    target = payload.get("target_actor_id")
    if target is not None and (target == actor_id or target not in valid_ids):
        return {**payload, "target_actor_id": None}
    return payload


def lod_after_perception(
    lod: ActorLod,
    items: list[dict[str, Any]],
    tick: int,
    *,
    high_intensity: float = HIGH_INTENSITY,
) -> ActorLod:
    """지각한 항목들 → LOD 갱신 (ADR-011 §관심 신호).

    Hot 승격 신호 셋: 응답 의무가 있는 상호작용(dm/comment — 상호작용 우선,
    ADR-012 규칙 2), Director의 사적 지목(nudge 관측 — 반응을 기대하고 심은
    지각이라 잠든 채 두면 다음 due까지 썩는다, ADR-013), 고강도 세계 사건
    (내 삶을 흔든 사건은 곧바로 반응하게 한다 — 임계는 high_intensity, 운영
    설정으로 조정 가능). 그 밖의 지각(반응·저강도 사건)은 관심 신호 —
    티어는 유지하고 강등 타이머만 리셋한다(touch).
    """
    for envelope in items:
        if envelope["type"] in _REPLYABLE or envelope["type"] == OBSERVATION_TYPE:
            return promote(lod, tick)
        if (
            envelope["type"] == INCIDENT_TYPE
            and float(envelope["payload"].get("intensity", 0.0)) >= high_intensity
        ):
            return promote(lod, tick)
    return touch(lod, tick)


class ActorPhases:
    """등록된 페르소나들을 tick 파이프라인에 태운다.

    Phase 1: 워커 in-process 실행 (단일 세계, 소수 액터). 샤드 워커 분리는
    ADR-012의 다음 증분이다 — 메일박스는 Redis에 있으므로 분리 시 그대로 간다.
    """

    def __init__(
        self,
        personas: list[Persona],
        *,
        ai: AiRuntimeClient,
        memory: WorkingMemory,
        mailbox: Mailbox | None = None,
        emotion: EmotionAdapter | None = None,
        relationship: RelationshipAdapter | None = None,
        semantic: SemanticMemory | None = None,
        goal: GoalAdapter | None = None,
        importance_weights: ImportanceWeights | None = None,
        belief_ledger: BeliefLedger | None = None,
        reflection_interval: int = 30,
        identity_redis: Redis | None = None,
        arc: ArcStore | None = None,
        decay_ledger: DecayLedger | None = None,
        promote_intensity: float = HIGH_INTENSITY,
    ) -> None:
        if not personas:
            raise ValueError("액터가 없다 — 최소 1명의 페르소나가 필요하다")
        self._personas = {p.id: p for p in personas}
        self._ai = ai
        self._memory = memory
        #: Director의 인생 아크 저장 — 있으면 decide 컨텍스트에 방향을 주입한다 (ADR-013)
        self._arc = arc
        self._mailbox = mailbox
        self._emotion = emotion
        self._relationship = relationship
        self._semantic = semantic
        self._goal = goal
        self._weights = importance_weights or ImportanceWeights()
        self._ledger = belief_ledger
        self._reflection_interval = max(1, reflection_interval)
        #: 잠든 액터도 깨우는 세계 사건 강도 임계 (ADR-011 §관심 신호, 운영 노브)
        self._promote_intensity = promote_intensity
        # 정체성 선언 1회 발행 가드 — Redis SETNX(재시작·다중 워커) + in-memory(tick당 재확인 회피)
        self._identity_redis = identity_redis
        self._declared: set[str] = set()
        # 첫 액터들은 세계의 주인공 — Hot으로 시작 (승격/강등은 관심 신호 소스가 생기면)
        self._lods: dict[str, ActorLod] = {
            actor_id: ActorLod(tier=Tier.HOT, last_interest_tick=0) for actor_id in self._personas
        }
        self._intents: list[tuple[str, str, dict[str, Any]]] = []  # (actor_id, tier, payload)
        #: 이번 tick에 응답할 상호작용: (actor_id, 원인 봉투, 답장 텍스트)
        self._replies: list[tuple[str, dict[str, Any], str]] = []
        #: perceive가 채우는 tick당 수신함
        self._inbox: dict[str, list[dict[str, Any]]] = {}
        #: perceive의 감정 평가 결과 — RESOLVE에서 engine.emotion으로 적재 (ADR-015)
        self._shifts: list[PendingShift] = []
        #: RESOLVE가 남기는 이번 tick의 응고 재료 — CONSOLIDATE의 관계·기억 입력 (ADR-008/016)
        self._resolved_actions: list[tuple[str, dict[str, Any]]] = []  # 대상 있는 행동 (관계용)
        self._resolved_action_all: list[tuple[str, dict[str, Any]]] = []  # 전 행동 (목표용)
        self._resolved_shifts: list[PendingShift] = []
        self._resolved_replies: list[tuple[str, dict[str, Any], str]] = []
        #: 액터별 감쇠가 적용된 마지막 tick — Cold 배치의 장부 (ADR-012 §Cold 티어 처리).
        #: decay_ledger(있으면)가 Redis에 영속해 워커 재시작에도 경과가 이어진다.
        #: 없으면 in-memory만 — 재시작한 액터는 현재 tick부터 다시 센다 (dev/테스트 허용 오차)
        self._last_decay: dict[str, int] = {}
        self._decay_ledger = decay_ledger
        self._ledger_loaded = decay_ledger is None  # 장부 없으면 수화할 것도 없다
        #: 이번 tick에 장부가 갱신된 액터들 — CONSOLIDATE 끝에 한 번에 영속한다
        self._ledger_dirty: set[str] = set()
        #: 재기상 정산(catch-up)의 관계 감쇠 발행분 — CONSOLIDATE 관계 적재에 합류
        self._rel_catchup: list[PendingRelEvent] = []

    async def schedule(self, ctx: TickContext) -> dict[str, int]:
        return scheduled_counts(self._lods, ctx.tick)

    async def world(self, ctx: TickContext) -> None:
        """정체성 선언 — 액터가 세계에 등장하며 이름·소개·목표를 read 모델에 노출한다.

        세계당 1회(SETNX). 환경 이벤트·Director 개입은 별도 엔진(ADR-013)의 몫이다.
        """
        if self._identity_redis is None:
            return
        for actor_id in sorted(self._personas):
            if actor_id in self._declared:
                continue
            marker = f"lf:iddecl:{ctx.world_id}:{actor_id}"
            if await self._identity_redis.set(marker, "1", nx=True):
                await self._declare_identity(ctx, self._personas[actor_id])
            self._declared.add(actor_id)

    async def _declare_identity(self, ctx: TickContext, persona: Persona) -> None:
        bio = " ".join(persona.identity_core.split())[:_BIO_MAX]
        goals = [
            {"description": str(g["description"])[:200], "priority": float(g.get("priority", 0.5))}
            for g in persona.goals
            if g.get("description")
        ]
        head = await current_head(ctx.conn, ctx.world_id, "actor", persona.id)
        await append(
            ctx.conn, PRINCIPAL,
            [
                NewEvent(
                    world_id=ctx.world_id,
                    stream="actor",
                    stream_key=persona.id,
                    type=IDENTITY_TYPE,
                    tick=ctx.tick,
                    actor_id=persona.id,
                    payload={
                        "name": persona.name,
                        "archetype": persona.archetype or "unknown",
                        "bio": bio or persona.name,
                        "goals": goals,
                    },
                )
            ],
            expected_head=head,
        )
        logger.info("정체성 선언: %s (%s) tick=%d", persona.id, persona.name, ctx.tick)

    async def perceive(self, ctx: TickContext) -> None:
        """메일박스 drain → appraise — 개입이 지각과 감정으로 들어온다 (ADR-012/015).

        지각은 곧 관심 신호다 — 지각한 액터는 승격/유지(LOD), 지각 없는 액터는
        유휴 강등(Hot→Warm→Cold, 히스테리시스). 강등이 곧 비용 정책이다 (ADR-011).
        """
        self._inbox = {}
        self._shifts = []
        if not self._ledger_loaded:
            await self._hydrate_ledger(ctx.world_id)
        for actor_id in self._personas:
            items = await self._mailbox.drain(ctx.world_id, actor_id) if self._mailbox else []
            # Director의 제어 신호(승격·아크)는 지각이 아니다 — 지각 항목과 분리한다 (ADR-013)
            promoted = any(e["type"] == SPOTLIGHT_TYPE for e in items)
            arcs = [e for e in items if e["type"] == ARC_TYPE]
            items = [e for e in items if e["type"] not in (SPOTLIGHT_TYPE, ARC_TYPE)]
            if arcs and self._arc is not None:
                # 아크는 지각·LOD와 무관한 배경 프레임 — 저장만 하고 다음 decide부터 스민다
                last = arcs[-1]["payload"]  # 여러 개면 마지막 계획이 권위다
                await self._arc.set(ctx.world_id, actor_id, last["stage"], last["intention"])
                logger.info(
                    "인생 아크 수신: %s stage=%s tick=%d", actor_id, last["stage"], ctx.tick
                )
            # LOD 갱신: 승격이 최우선(Hot), 없으면 지각 규칙, 지각도 없으면 유휴 강등 (ADR-011)
            if promoted:
                self._lods[actor_id] = promote(self._lods[actor_id], ctx.tick)
            elif items:
                self._lods[actor_id] = lod_after_perception(
                    self._lods[actor_id], items, ctx.tick,
                    high_intensity=self._promote_intensity,
                )
            else:
                self._lods[actor_id] = maybe_demote(self._lods[actor_id], ctx.tick)
            if promoted or items:
                # 잠들었던 액터가 깨어난다 — 밀린 감쇠를 상태 사용 전에 정산 (ADR-012)
                await self._catch_up(ctx, actor_id)
            if not items:
                continue  # 승격 신호는 기억·감정·목표에 들어가지 않는다 (메타 제어)
            self._inbox[actor_id] = items
            for envelope in items:
                await self._memory.add(ctx.world_id, actor_id, describe_interaction(envelope))
            if self._emotion is not None:
                shifts, mood_line = await self._emotion.appraise(
                    ctx.world_id, self._personas[actor_id], items, tick=ctx.tick
                )
                self._shifts.extend(shifts)
                # 현재 감정이 다음 결정의 컨텍스트가 된다 (ADR-015 §행동 연결)
                await self._memory.add(ctx.world_id, actor_id, mood_line)
            # 지지·관심은 소속 욕구를 채운다 (ADR-012 need 갱신)
            if self._goal is not None:
                for envelope in items:
                    await self._goal.record_interaction(
                        ctx.world_id, self._personas[actor_id], envelope["type"]
                    )
            logger.info("지각: %s 에게 플레이어 개입 %d건", actor_id, len(items))

    async def decide(self, ctx: TickContext) -> dict[str, int]:
        self._intents = []
        self._replies = []
        decided = {"hot": 0, "warm": 0, "cold": 0}
        due = due_by_tier(self._lods, ctx.tick)
        world = WorldContext(world_id=ctx.world_id, tick=ctx.tick, world_time=ctx.world_time)
        schema = registry.payload_schema(ACTION_TYPE)

        for tier in (Tier.HOT, Tier.WARM):
            for actor_id in due[tier]:
                persona = self._personas[actor_id]
                working = await self._memory.recent(ctx.world_id, actor_id)
                arc = await self._arc_of(ctx.world_id, actor_id)
                # 현재 욕구·목표를 결정 앞에 세운다 — 액터가 자기 목표를 좇게 (ADR-012).
                # 아크(있으면)가 미는 욕구의 목표가 앞자리다 (plan/08 전환점 사슬)
                if self._goal is not None:
                    summary = await self._goal.summary(
                        ctx.world_id, persona,
                        arc_stage=arc.stage if arc is not None else None,
                    )
                    working = [summary, *working]
                episodes = await self._recall(ctx.world_id, actor_id, working)
                relationships = await self._relationship_summary(ctx.world_id, actor_id)
                bundle = build(
                    persona, working, world,
                    episodes=episodes, arc=arc, relationships=relationships,
                )
                payload = await self._ai.decide_action(
                    bundle, schema, tier=tier.value, actor_id=actor_id, tick=ctx.tick
                )
                if payload is None:
                    payload = fallback_action(persona, ctx.tick, bundle.trace_id)
                # LLM이 지어낸 대상을 소스에서 끊는다 — 피드·관계·그래프로 번지기 전에
                payload = sanitize_target(payload, set(self._personas), actor_id)
                self._intents.append((actor_id, tier.value, payload))
                decided[tier.value] += 1

        # 플레이어 상호작용 응답 — due 여부와 무관하게 반드시 (상호작용 우선)
        for actor_id in sorted(self._inbox):
            persona = self._personas[actor_id]
            for envelope in self._inbox[actor_id]:
                if envelope["type"] not in _REPLYABLE:
                    continue
                working = await self._memory.recent(ctx.world_id, actor_id)
                episodes = await self._recall(ctx.world_id, actor_id, working)
                # 이 플레이어와의 대화를 시간순으로 — 답장이 흐름을 잇게 한다 (ADR-009)
                conversation = conversation_turns(working, envelope["payload"]["player_id"])
                # 답장도 결정이다 — 인생 방향이 대화의 결까지 물들인다 (ADR-013)
                arc = await self._arc_of(ctx.world_id, actor_id)
                # 관계의 온도도 — 앙금이 남은 상대에겐 답의 결이 달라야 한다 (ADR-009 §3)
                relationships = await self._relationship_summary(ctx.world_id, actor_id)
                bundle = build(
                    persona, working, world, purpose="reply_to_player",
                    episodes=episodes, conversation=conversation, arc=arc,
                    relationships=relationships,
                )
                text = await self._ai.converse(
                    bundle, tier="hot", actor_id=actor_id, tick=ctx.tick
                )
                if text is None:
                    text = fallback_reply(persona, envelope["payload"]["text"])
                self._replies.append((actor_id, envelope, text))

        # Cold 티어 — 통계 일괄 처리(ADR-012): LLM 없이 일과 행동만, due일 때만(100 tick
        # 케이던스). 잠든 기간의 생활 요약이라 스팸 없이 삶이 이어진다 — 비용 near-zero.
        for actor_id in due[Tier.COLD]:
            persona = self._personas[actor_id]
            # 아크(있으면)가 일과의 결이 된다 — 잠든 삶도 방향이 있다 (ADR-013/plan-08)
            arc = await self._arc_of(ctx.world_id, actor_id)
            payload = routine_action(persona, ctx.tick, f"cold-{actor_id}-{ctx.tick}", arc=arc)
            payload = sanitize_target(payload, set(self._personas), actor_id)
            self._intents.append((actor_id, Tier.COLD.value, payload))
            decided["cold"] += 1
        return decided

    async def _recall(self, world_id: str, actor_id: str, working: list[str]) -> list[str]:
        """장기 기억 회상 — 최신 지각을 질의로 유사 에피소드 top-k (ADR-008)."""
        if self._semantic is None or not working:
            return []
        return await self._semantic.recall(world_id, actor_id, working[0], k=3)

    async def _arc_of(self, world_id: str, actor_id: str) -> Arc | None:
        """Director가 준 인생 아크 — 없으면 None (아직 아크 없는 액터는 일상을 산다)."""
        if self._arc is None:
            return None
        return await self._arc.get(world_id, actor_id)

    async def _relationship_summary(self, world_id: str, actor_id: str) -> str | None:
        """관계 요약 — decide의 Relationship(3) 섹션 재료 (ADR-009 §3, ADR-016).

        이름은 페르소나 명부로 그라운딩한다 — 플레이어 id는 명부에 없어 id
        그대로 (기존 관례). 액터당 Redis 조회가 늘지만 Phase 1(소수 액터) 허용.
        """
        if self._relationship is None:
            return None
        names = {p.id: p.name for p in self._personas.values()}
        return await self._relationship.summary(world_id, actor_id, names)

    async def _hydrate_ledger(self, world_id: str) -> None:
        """감쇠 장부 수화 — 재시작 후 첫 tick, 잠든 액터의 경과를 되찾는다 (ADR-012)."""
        assert self._decay_ledger is not None
        stored = await self._decay_ledger.load_all(world_id)
        for actor_id, tick in stored.items():
            if actor_id in self._personas:
                self._last_decay.setdefault(actor_id, tick)
        self._ledger_loaded = True
        if stored:
            logger.info("감쇠 장부 수화: %d 액터 (재시작에도 경과가 이어진다)", len(stored))

    def _ledger_get(self, actor_id: str, default: int) -> int:
        """장부 조회 — 처음 보는 액터는 default로 시작하고 영속 대상에 올린다."""
        if actor_id not in self._last_decay:
            self._last_decay[actor_id] = default
            self._ledger_dirty.add(actor_id)
        return self._last_decay[actor_id]

    def _ledger_set(self, actor_id: str, tick: int) -> None:
        self._last_decay[actor_id] = tick
        self._ledger_dirty.add(actor_id)

    async def _catch_up(self, ctx: TickContext, actor_id: str) -> None:
        """잠들었던 액터의 밀린 감쇠 정산 — 직전 tick(T-1)까지 (ADR-012 §Cold 배치).

        지각·결정이 stale 상태를 쓰지 않게 상태 사용 전에 부른다. 이번 tick 몫은
        CONSOLIDATE가 마저 적용한다. 관계 감쇠의 발행 임계 초과분은 버퍼에 모아
        CONSOLIDATE의 관계 적재에 합류시킨다 (조용한 유실 금지).
        """
        behind = (ctx.tick - 1) - self._ledger_get(actor_id, ctx.tick - 1)
        if behind <= 0:
            return
        persona = self._personas[actor_id]
        if self._emotion is not None:
            await self._emotion.decay_ticks(ctx.world_id, persona, behind)
        if self._goal is not None:
            await self._goal.decay_ticks(ctx.world_id, persona, behind)
        if self._relationship is not None:
            self._rel_catchup += await self._relationship.decay_all(
                ctx.world_id, actor_id, ticks=behind
            )
        self._ledger_set(actor_id, ctx.tick - 1)
        logger.info("감쇠 정산: %s 밀린 %d tick 적용 (재기상)", actor_id, behind)

    def _decay_plan(self, ctx: TickContext) -> dict[str, int]:
        """이번 tick에 감쇠할 액터 → 적용할 tick 수 (ADR-012 §Cold 티어 처리).

        Hot/Warm은 매 tick 1씩. Cold는 잠들어 있다 — due tick(100 케이던스)에만
        경과분을 한 번에 적용한다(세 감쇠 모두 tick 수에 대해 등가 합성). 상태를
        건드리지 않는 tick엔 Redis 왕복도 없다 — 강등이 곧 비용 정책 (ADR-011)."""
        plan: dict[str, int] = {}
        for actor_id in self._personas:
            # 장부 초기화는 스킵 여부와 무관 — 잠들기 시작한 시점을 세어야 경과가 맞는다
            last = self._ledger_get(actor_id, ctx.tick - 1)
            lod = self._lods[actor_id]
            if lod.tier is Tier.COLD and not is_due(actor_id, lod, ctx.tick):
                continue  # 잠든 액터 — due tick에 경과분을 한 번에
            if (elapsed := ctx.tick - last) > 0:
                plan[actor_id] = elapsed
        return plan

    def _reply_event(
        self, ctx: TickContext, actor_id: str, source: dict[str, Any], text: str
    ) -> NewEvent:
        channel = _REPLYABLE[source["type"]]
        return NewEvent(
            world_id=ctx.world_id,
            stream="actor",
            stream_key=actor_id,
            type=MESSAGE_TYPE,
            tick=ctx.tick,
            actor_id=actor_id,
            causation_id=source["event_id"],
            # 개입이 시작한 서사 사슬을 잇는다 — '당신이 시작한 이야기' (ADR-013)
            correlation_id=source["correlation_id"],
            payload={
                "channel": channel,
                "target_player_id": source["payload"]["player_id"],
                "text": text[:1000],
                "post_id": source["payload"].get("post_id"),
                "in_reply_to": source["event_id"],
            },
        )

    async def resolve(self, ctx: TickContext) -> int:
        """충돌 해소 → 확정 이벤트 적재. actor_id 순 순차·결정적 (ADR-011 §4).

        같은 액터의 응답과 행동은 한 번의 append(단일 스트림 CAS)로 묶인다 —
        응답이 행동보다 앞선다 (상호작용 우선, ADR-012 규칙 2).
        """
        self._resolved_actions = []
        self._resolved_action_all = []
        events_by_actor: dict[str, list[NewEvent]] = {}
        memos: dict[str, list[str]] = {}

        for actor_id, envelope, text in self._replies:
            events_by_actor.setdefault(actor_id, []).append(
                self._reply_event(ctx, actor_id, envelope, text)
            )
            player = envelope["payload"]["player_id"]
            memos.setdefault(actor_id, []).append(
                f"tick {ctx.tick}: 나는 플레이어 {player}에게 답했다 — \"{text}\""
            )

        for actor_id, _tier, payload in self._intents:
            events_by_actor.setdefault(actor_id, []).append(
                NewEvent(
                    world_id=ctx.world_id,
                    stream="actor",
                    stream_key=actor_id,
                    type=ACTION_TYPE,
                    tick=ctx.tick,
                    actor_id=actor_id,
                    payload=payload,
                )
            )
            memos.setdefault(actor_id, []).append(
                f"tick {ctx.tick}: 나는 {payload['action_kind']} — {payload['intent']}"
            )

        # 감정 변화 먼저 — 응답·행동의 원인 상태가 앞서 기록된다 (ADR-015)
        shifts_by_actor: dict[str, list[PendingShift]] = {}
        for shift in self._shifts:
            shifts_by_actor.setdefault(shift.actor_id, []).append(shift)

        emitted = 0
        for actor_id in sorted(set(events_by_actor) | set(shifts_by_actor)):
            head = await current_head(ctx.conn, ctx.world_id, "actor", actor_id)
            shifts = shifts_by_actor.get(actor_id, [])
            if shifts:
                stored = await append(
                    ctx.conn,
                    EMOTION_PRINCIPAL,
                    [
                        NewEvent(
                            world_id=ctx.world_id,
                            stream="actor",
                            stream_key=actor_id,
                            type=SHIFT_TYPE,
                            tick=ctx.tick,
                            actor_id=actor_id,
                            causation_id=s.causation_id,
                            correlation_id=s.correlation_id,
                            payload=s.payload,
                        )
                        for s in shifts
                    ],
                    expected_head=head,
                )
                emitted += len(stored)
                head += len(stored)
            events = events_by_actor.get(actor_id)
            if not events:
                continue
            stored = await append(ctx.conn, PRINCIPAL, events, expected_head=head)
            emitted += len(stored)
            for record in stored:
                env = record.envelope
                if env["type"] == ACTION_TYPE:
                    self._resolved_action_all.append((actor_id, env))  # 목표 응고용 (전 행동)
                    # 대상 있는 행동은 관계 응고의 재료다 (CONSOLIDATE, ADR-016 규칙 1)
                    if env["payload"].get("target_actor_id"):
                        self._resolved_actions.append((actor_id, env))
            for memo in memos[actor_id]:
                # 자기 행동/응답 → Working Memory 유입 (지각의 최소 형태, ADR-008)
                await self._memory.add(ctx.world_id, actor_id, memo)
            logger.info(
                "확정: %s tick=%d 이벤트 %d건 (응답 %d) head=%d",
                actor_id, ctx.tick, len(stored),
                sum(1 for e in events if e.type == MESSAGE_TYPE),
                stored[-1].stream_seq,
            )

        self._intents = []
        self._resolved_replies = list(self._replies)  # 기억 응고용 스냅샷 (ADR-008)
        self._replies = []
        self._resolved_shifts = list(self._shifts)  # 관계 응고용 스냅샷 (ADR-016 규칙 2)
        self._shifts = []
        return emitted

    async def consolidate(self, ctx: TickContext) -> None:
        # Cold 배치 계획 — 잠든 액터는 감쇠 스킵, due 액터는 경과분 일괄 (ADR-012)
        decay_plan = self._decay_plan(ctx)
        # 관계 응고 (ADR-016 §갱신 규칙 — CONSOLIDATE 단계)
        rel_counts: dict[str, int] = {}
        if self._relationship is not None:
            rel_counts = await self._consolidate_relationships(ctx, decay_plan)
        # 목표 응고 — 행동이 욕구를 채우고 목표를 진행시킨다 (ADR-012 need/goal)
        goal_relevance, goal_actions = await self._consolidate_goals(ctx)
        # 기억 응고 — 이번 tick의 재료가 에피소드로 접힌다 (ADR-008)
        await self._consolidate_memories(ctx, rel_counts, goal_relevance, goal_actions)
        # 감정 감쇠·baseline 회귀 (ADR-015 §감쇠) — Cold는 배치로 (ADR-012).
        # reflection보다 먼저 — 곱씹음은 이 tick까지 정산된 상태를 읽어야 한다
        if self._emotion is not None:
            for actor_id, ticks in sorted(decay_plan.items()):
                await self._emotion.decay_ticks(ctx.world_id, self._personas[actor_id], ticks)
        # 욕구 감쇠 — 만족은 되돌아온다 (ADR-012)
        if self._goal is not None:
            for actor_id, ticks in sorted(decay_plan.items()):
                await self._goal.decay_ticks(ctx.world_id, self._personas[actor_id], ticks)
        # reflection — 주기적으로 상태의 패턴이 신념이 된다 (ADR-008).
        # 잠든(Cold 비-due) 액터는 곱씹지 않는다 — 미정산 상태로 신념을 세우지 않는다
        if ctx.tick > 0 and ctx.tick % self._reflection_interval == 0:
            await self._reflect(ctx, decay_plan)
        # 감쇠 장부 갱신 — 이 tick까지 정산됐다. 갱신분만 일괄 영속 (재시작 연속성)
        for actor_id in decay_plan:
            self._ledger_set(actor_id, ctx.tick)
        if self._decay_ledger is not None and self._ledger_dirty:
            await self._decay_ledger.mark(
                ctx.world_id, {a: self._last_decay[a] for a in self._ledger_dirty}
            )
        self._ledger_dirty.clear()
        self._resolved_actions = []
        self._resolved_action_all = []
        self._resolved_shifts = []
        self._resolved_replies = []

    async def _consolidate_goals(
        self, ctx: TickContext
    ) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
        """행동 → 욕구·목표 갱신. 반환: (액터별 congruence, 목표 진행시킨 행동 봉투).

        congruence는 기억 중요도의 goal 항이 되고, 진행이 임계를 넘긴 목표는
        actor.goal.advanced로 적재되며 그 행동은 에피소드 재료가 된다 (대상 없어도).
        """
        if self._goal is None:
            return {}, {}
        relevance: dict[str, float] = {}
        advanced_actions: dict[str, dict[str, Any]] = {}
        by_actor: dict[str, list[PendingGoalEvent]] = {}
        #: 목표 진전 → 기쁨의 근거: actor → (최대 congruence, 원인 행동)
        joy_cause: dict[str, tuple[float, dict[str, Any]]] = {}
        #: 이번 tick에 목표를 완주한 액터 → 원인 행동 (강한 기쁨의 근거)
        achieved_cause: dict[str, dict[str, Any]] = {}
        for actor_id, envelope in self._resolved_action_all:
            congruence, events, achieved = await self._goal.record_action(
                ctx.world_id, self._personas[actor_id], envelope
            )
            relevance[actor_id] = max(relevance.get(actor_id, 0.0), congruence)
            if events:
                advanced_actions[actor_id] = envelope
                by_actor.setdefault(actor_id, []).extend(events)
                prior = joy_cause.get(actor_id)
                if prior is None or congruence > prior[0]:
                    joy_cause[actor_id] = (congruence, envelope)
            if achieved:
                achieved_cause[actor_id] = envelope

        for actor_id in sorted(by_actor):
            head = await current_head(ctx.conn, ctx.world_id, "actor", actor_id)
            events = [
                NewEvent(
                    world_id=ctx.world_id,
                    stream="actor",
                    stream_key=actor_id,
                    type=e.type,  # actor.goal.advanced 또는 actor.goal.achieved
                    tick=ctx.tick,
                    actor_id=actor_id,
                    causation_id=e.causation_id,
                    correlation_id=e.correlation_id,
                    payload=e.payload,
                )
                for e in by_actor[actor_id]
            ]
            await append(ctx.conn, GOAL_PRINCIPAL, events, expected_head=head)
            logger.info("목표 이벤트 적재: %s %d건 tick=%d", actor_id, len(events), ctx.tick)

        # 목표 결과 → 감정 (ADR-015 goal_congruence): 완주는 큰 기쁨, 진전은 기쁨, 결핍은 괴로움
        if self._emotion is not None:
            acted = sorted({actor_id for actor_id, _ in self._resolved_action_all})
            await self._emit_goal_emotions(ctx, joy_cause, achieved_cause, acted)
        return relevance, advanced_actions

    async def _emit_goal_emotions(
        self,
        ctx: TickContext,
        joy_cause: dict[str, tuple[float, dict[str, Any]]],
        achieved_cause: dict[str, dict[str, Any]],
        acted: list[str],
    ) -> None:
        """행동한 액터별로 목표-감정 신호를 모아 actor.emotion.shifted로 적재한다."""
        assert self._emotion is not None and self._goal is not None
        for actor_id in acted:
            persona = self._personas[actor_id]
            signals: list[tuple[str, float, str | None, str | None, str | None]] = []
            if actor_id in achieved_cause:
                # 완주는 서사의 마디 — 강한 기쁨 (magnitude 1.0)
                env = achieved_cause[actor_id]
                signals.append(
                    ("goal.achieved", 1.0, env["event_id"],
                     env["event_id"], env.get("correlation_id"))
                )
            elif actor_id in joy_cause:
                congruence, env = joy_cause[actor_id]
                signals.append(
                    ("goal.advanced", congruence, env["event_id"],
                     env["event_id"], env.get("correlation_id"))
                )
            starve = await self._goal.starvation_signal(ctx.world_id, persona)
            if starve is not None:
                signals.append(("goal.frustrated", starve[1], None, None, None))
            if not signals:
                continue
            shifts = await self._emotion.appraise_goal_signals(ctx.world_id, persona, signals)
            if not shifts:
                continue
            head = await current_head(ctx.conn, ctx.world_id, "actor", actor_id)
            await append(
                ctx.conn, EMOTION_PRINCIPAL,
                [
                    NewEvent(
                        world_id=ctx.world_id,
                        stream="actor",
                        stream_key=actor_id,
                        type=SHIFT_TYPE,
                        tick=ctx.tick,
                        actor_id=actor_id,
                        causation_id=s.causation_id,
                        correlation_id=s.correlation_id,
                        payload=s.payload,
                    )
                    for s in shifts
                ],
                expected_head=head,
            )

    async def _reflect(self, ctx: TickContext, decay_plan: dict[str, int]) -> None:
        """경험 → 신념 (ADR-008 reflection). 두 경로가 한 저장 계약을 공유한다:

        규칙(derive_beliefs)은 상태 패턴에서 — 결정적, 언제나 돈다.
        LLM(_llm_insight)은 작업 기억을 곱씹은 통찰 하나를 더한다 — 미지원·실패면
        조용히 생략 (규칙 신념이 바닥을 지킨다).

        decay_plan에 없는 액터(잠든 Cold)는 건너뛴다 — 감쇠 미정산 상태로
        신념을 세우면 최대 100 tick 낡은 세계관이 굳는다 (ADR-012 Cold 정합).
        깨어나는 tick(정산 후)에 곱씹는다.
        """
        if self._emotion is None or self._relationship is None or self._ledger is None:
            return
        names = {p.id: p.name for p in self._personas.values()}
        for actor_id in sorted(decay_plan):
            emotion_state = await self._emotion.load(ctx.world_id, actor_id)
            edges = {}
            for other_id in await self._relationship.counterparts(ctx.world_id, actor_id):
                state = await self._relationship.load(ctx.world_id, actor_id, other_id)
                if state is not None:
                    edges[other_id] = state
            beliefs = derive_beliefs(emotion_state, edges, name_map=names)
            # 신념 폐기 — 근거 상태가 무너진 발행 슬롯은 철회문으로 갱신된다 (ADR-008)
            published = await self._ledger.entries(ctx.world_id, actor_id)
            beliefs += retract_stale(published, beliefs, name_map=names)
            insight = await self._llm_insight(ctx, actor_id, set(edges), names)
            if insight is not None:
                beliefs = [*beliefs, insight]
            for belief in beliefs:
                if not await self._ledger.changed(ctx.world_id, actor_id, belief):
                    continue
                await self._store_belief(ctx, actor_id, belief)
                # 인물 통찰은 관계 비중에 스민다 — 그 사람이 마음에서 자리를 차지한다
                # (ADR-016, 엣지가 있을 때만 — 생각만으로 관계가 시작되진 않는다)
                if belief.kind == "person_insight" and belief.about_id:
                    await self._relationship.record_insight(
                        ctx.world_id, actor_id, belief.about_id, belief.confidence
                    )

    async def _llm_insight(
        self, ctx: TickContext, actor_id: str, counterparts: set[str], names: dict[str, str]
    ) -> Belief | None:
        """작업 기억을 곱씹은 LLM 통찰 하나 — "이 경험들이 의미하는 것" (ADR-008).

        대상 후보는 아는 사람(관계 상대 + 동료 액터)뿐 — 환각 대상은 insight_to_belief가
        끊는다. 곱씹을 경험이 없으면 묻지 않는다 (빈 기억에서 통찰은 안 나온다).
        """
        working = await self._memory.recent(ctx.world_id, actor_id)
        if not working:
            return None
        known = sorted(counterparts | (set(self._personas) - {actor_id}))
        roster = ", ".join(f"{names.get(a, a)}({a})" for a in known) or "(없음)"
        world = WorldContext(world_id=ctx.world_id, tick=ctx.tick, world_time=ctx.world_time)
        bundle = build(
            self._personas[actor_id], [f"아는 사람들: {roster}", *working],
            world, purpose="reflect",
        )
        output = await self._ai.reflect(
            bundle, insight_schema(known), actor_id=actor_id, tick=ctx.tick
        )
        if output is None:
            return None
        belief = insight_to_belief(output, set(known))
        if belief is not None:
            logger.info("LLM 통찰: %s [%s] conf=%.2f", actor_id, belief.kind, belief.confidence)
        return belief

    async def _store_belief(self, ctx: TickContext, actor_id: str, belief: Belief) -> None:
        head = await current_head(ctx.conn, ctx.world_id, "actor", actor_id)
        [stored] = await append(
            ctx.conn, PRINCIPAL,
            [
                NewEvent(
                    world_id=ctx.world_id,
                    stream="actor",
                    stream_key=actor_id,
                    type=BELIEF_TYPE,
                    tick=ctx.tick,
                    actor_id=actor_id,
                    causation_id=belief.source_event_ids[0] if belief.source_event_ids else None,
                    payload={
                        "statement": belief.statement,
                        "kind": belief.kind,
                        "confidence": belief.confidence,
                        "about_id": belief.about_id,
                        "source_event_ids": belief.source_event_ids,
                    },
                )
            ],
            expected_head=head,
        )
        assert self._ledger is not None
        await self._ledger.record(ctx.world_id, actor_id, belief)
        if self._semantic is not None:
            # 같은 (kind, about) 신념은 같은 포인트를 덮어쓴다 — 신념은 갱신되는 자리다
            await self._semantic.remember(
                ctx.world_id, actor_id,
                event_id=stored.envelope["event_id"],
                text=belief.statement,
                importance=belief.confidence,
                source_event_ids=belief.source_event_ids,
                point_key=belief_point_key(actor_id, belief),
            )
        await self._memory.add(ctx.world_id, actor_id, f"곱씹은 생각: {belief.statement}")
        logger.info(
            "신념 형성: %s [%s→%s] conf=%.2f tick=%d",
            actor_id, belief.kind, belief.about_id, belief.confidence, ctx.tick,
        )

    async def _consolidate_memories(
        self,
        ctx: TickContext,
        rel_counts: dict[str, int],
        goal_relevance: dict[str, float],
        goal_actions: dict[str, dict[str, Any]],
    ) -> None:
        actions_by_actor = {actor_id: env for actor_id, env in self._resolved_actions}
        # 목표를 진행시킨 행동은 대상이 없어도 기억할 일이다 ("사이드 프로젝트를 진행했다")
        actions_by_actor.update(goal_actions)
        peaks: dict[str, float] = {}
        for shift in self._resolved_shifts:
            intensity = float(shift.instance.get("intensity", 0.0))
            peaks[shift.actor_id] = max(peaks.get(shift.actor_id, 0.0), intensity)

        for actor_id in sorted(self._personas):
            materials = TickMaterials(
                interactions=self._inbox.get(actor_id, []),
                replies=[
                    (env, text) for a, env, text in self._resolved_replies if a == actor_id
                ],
                action_envelope=actions_by_actor.get(actor_id),
                emotion_peak=peaks.get(actor_id, 0.0),
                relationship_events=rel_counts.get(actor_id, 0),
                goal_relevance=goal_relevance.get(actor_id, 0.0),
            )
            episode = build_episode(materials, weights=self._weights)
            if episode is None:
                continue
            await self._store_episode(ctx, actor_id, episode)

    async def _store_episode(self, ctx: TickContext, actor_id: str, episode: Episode) -> None:
        head = await current_head(ctx.conn, ctx.world_id, "actor", actor_id)
        [stored] = await append(
            ctx.conn, PRINCIPAL,
            [
                NewEvent(
                    world_id=ctx.world_id,
                    stream="actor",
                    stream_key=actor_id,
                    type=MEMORY_TYPE,
                    tick=ctx.tick,
                    actor_id=actor_id,
                    causation_id=episode.causation_id,
                    correlation_id=episode.correlation_id,
                    payload={
                        "summary": episode.summary,
                        "importance": episode.importance,
                        "factors": episode.factors,
                        "source_event_ids": episode.source_event_ids,
                        "tags": episode.tags,
                    },
                )
            ],
            expected_head=head,
        )
        # 중요도 게이트 통과분만 Semantic 임베딩 — 망각은 Semantic에서만 (ADR-008)
        if self._semantic is not None and episode.importance >= self._weights.semantic_gate:
            await self._semantic.remember(
                ctx.world_id, actor_id,
                event_id=stored.envelope["event_id"],
                text=episode.summary,
                importance=episode.importance,
                source_event_ids=episode.source_event_ids,
            )
        logger.info(
            "기억 응고: %s tick=%d importance=%.2f%s",
            actor_id, ctx.tick, episode.importance,
            " (semantic)" if episode.importance >= self._weights.semantic_gate else "",
        )

    async def _consolidate_relationships(
        self, ctx: TickContext, decay_plan: dict[str, int]
    ) -> dict[str, int]:
        assert self._relationship is not None
        rel = self._relationship
        # 재기상 정산(perceive catch-up)의 관계 감쇠 발행분 합류 (조용한 유실 금지)
        pending: list[PendingRelEvent] = self._rel_catchup
        self._rel_catchup = []

        # 규칙 1a: 플레이어 개입 → 액터→플레이어 엣지 (그가 내게 한 일).
        # 세계 사건 지각은 관계 갱신이 아니다 — 간접 효과(소문)는 후속 (ADR-016 규칙 3)
        for actor_id in sorted(self._inbox):
            for envelope in self._inbox[actor_id]:
                if not envelope["type"].startswith("player."):
                    continue
                pending += await rel.record_interaction(
                    ctx.world_id, actor_id, envelope["payload"]["player_id"],
                    envelope["type"], "incoming", cause=envelope,
                )

        # 규칙 1b: 대상 있는 액터 행동 → 양방향 엣지 (비대칭 델타)
        for actor_id, envelope in self._resolved_actions:
            target = envelope["payload"]["target_actor_id"]
            action_kind = envelope["payload"]["action_kind"]
            kind = f"action.{action_kind}"
            pending += await rel.record_interaction(
                ctx.world_id, actor_id, target, kind, "outgoing", cause=envelope
            )
            pending += await rel.record_interaction(
                ctx.world_id, target, actor_id, kind, "incoming", cause=envelope
            )
            # 상위 전이 행동(고백·절교) — stage는 수치가 아니라 이벤트가 만든다 (ADR-016)
            if action_kind in STAGE_ACTION_KINDS:
                pending += await rel.record_stage_action(
                    ctx.world_id, actor_id, target, action_kind, cause=envelope
                )

        # 규칙 2: 감정 응고 — 대상 있는 감정이 관계로 스며든다
        for shift in self._resolved_shifts:
            instance = shift.instance
            if instance.get("target_id"):
                pending += await rel.record_emotion(
                    ctx.world_id, shift.actor_id, instance["target_id"],
                    instance["type"], float(instance["intensity"]),
                    cause={
                        "event_id": shift.causation_id,
                        "correlation_id": shift.correlation_id,
                    },
                )

        # 규칙 4: 시간 감쇠 — Cold 배치 계획에 따라 (잠든 액터는 due tick에 몰아서)
        for actor_id in sorted(decay_plan):
            pending += await rel.decay_all(ctx.world_id, actor_id, ticks=decay_plan[actor_id])

        # 규칙 5: 임계 초과 변화만 적재 (pending은 이미 임계 통과분만 담고 있다)
        by_edge: dict[tuple[str, str], list[PendingRelEvent]] = {}
        for event in pending:
            by_edge.setdefault((event.from_id, event.to_id), []).append(event)
        for from_id, to_id in sorted(by_edge):
            stream_key = f"{from_id}|{to_id}"
            events = [
                NewEvent(
                    world_id=ctx.world_id,
                    stream="relationship",
                    stream_key=stream_key,
                    type=event.type,
                    tick=ctx.tick,
                    actor_id=event.from_id,
                    causation_id=event.causation_id,
                    correlation_id=event.correlation_id,
                    payload=event.payload,
                )
                for event in by_edge[(from_id, to_id)]
            ]
            head = await current_head(ctx.conn, ctx.world_id, "relationship", stream_key)
            await append(ctx.conn, REL_PRINCIPAL, events, expected_head=head)
            logger.info("관계 기록: %s→%s %d건 tick=%d", from_id, to_id, len(events), ctx.tick)

        # 기억 응고의 관계영향 항 입력 — from(액터)별 관계 이벤트 수
        counts: dict[str, int] = {}
        for event in pending:
            counts[event.from_id] = counts.get(event.from_id, 0) + 1
        return counts
