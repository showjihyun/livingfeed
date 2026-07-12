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
from lf_schemas import registry
from lf_tick.lod import ActorLod, Tier, due_by_tier, scheduled_counts
from lf_tick.pipeline import TickContext

from lf_actor.client import AiRuntimeClient
from lf_actor.context import WorldContext, build
from lf_actor.emotion import PRINCIPAL as EMOTION_PRINCIPAL
from lf_actor.emotion import SHIFT_TYPE, EmotionAdapter, PendingShift
from lf_actor.mailbox import Mailbox
from lf_actor.memory import WorkingMemory
from lf_actor.persona import Persona
from lf_actor.relationship import PRINCIPAL as REL_PRINCIPAL
from lf_actor.relationship import PendingRelEvent, RelationshipAdapter
from lf_actor.rules import fallback_action, fallback_reply

logger = logging.getLogger("lf.actor.phases")

PRINCIPAL = "engine.actor"
ACTION_TYPE = "actor.action.performed"
MESSAGE_TYPE = "actor.message.sent"

#: 응답 의무가 있는 상호작용 (반응(like)은 지각·감정 입력일 뿐 응답하지 않는다)
_REPLYABLE = {"player.dm.sent": "dm", "player.comment.posted": "comment"}


def describe_interaction(envelope: dict[str, Any]) -> str:
    """상호작용 봉투 → Working Memory 문장 (지각의 최소 형태)."""
    p = envelope["payload"]
    kind = envelope["type"]
    if kind == "player.dm.sent":
        return f"플레이어 {p['player_id']}의 DM: \"{p['text']}\""
    if kind == "player.comment.posted":
        return f"플레이어 {p['player_id']}가 내 글에 댓글을 남겼다: \"{p['text']}\""
    if kind == "player.reaction.added":
        return f"플레이어 {p['player_id']}가 내 글에 좋아요를 눌렀다"
    if kind == "world.incident.occurred":
        return f"세계 사건: {p['description']}"
    return f"플레이어 상호작용: {kind}"


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
    ) -> None:
        if not personas:
            raise ValueError("액터가 없다 — 최소 1명의 페르소나가 필요하다")
        self._personas = {p.id: p for p in personas}
        self._ai = ai
        self._memory = memory
        self._mailbox = mailbox
        self._emotion = emotion
        self._relationship = relationship
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
        #: RESOLVE가 남기는 이번 tick의 응고 재료 — CONSOLIDATE의 관계 갱신 입력 (ADR-016)
        self._resolved_actions: list[tuple[str, dict[str, Any]]] = []  # (actor_id, envelope)
        self._resolved_shifts: list[PendingShift] = []

    async def schedule(self, ctx: TickContext) -> dict[str, int]:
        return scheduled_counts(self._lods, ctx.tick)

    async def world(self, ctx: TickContext) -> None:
        return None  # 환경 이벤트/Director 개입은 ADR-013 단계에서

    async def perceive(self, ctx: TickContext) -> None:
        """메일박스 drain → appraise — 개입이 지각과 감정으로 들어온다 (ADR-012/015)."""
        self._inbox = {}
        self._shifts = []
        if self._mailbox is None:
            return
        for actor_id in self._personas:
            items = await self._mailbox.drain(ctx.world_id, actor_id)
            if not items:
                continue
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
                bundle = build(persona, working, world)
                payload = await self._ai.decide_action(
                    bundle, schema, tier=tier.value, actor_id=actor_id, tick=ctx.tick
                )
                if payload is None:
                    payload = fallback_action(persona, ctx.tick, bundle.trace_id)
                self._intents.append((actor_id, tier.value, payload))
                decided[tier.value] += 1

        # 플레이어 상호작용 응답 — due 여부와 무관하게 반드시 (상호작용 우선)
        for actor_id in sorted(self._inbox):
            persona = self._personas[actor_id]
            for envelope in self._inbox[actor_id]:
                if envelope["type"] not in _REPLYABLE:
                    continue
                working = await self._memory.recent(ctx.world_id, actor_id)
                bundle = build(persona, working, world, purpose="reply_to_player")
                text = await self._ai.converse(
                    bundle, tier="hot", actor_id=actor_id, tick=ctx.tick
                )
                if text is None:
                    text = fallback_reply(persona, envelope["payload"]["text"])
                self._replies.append((actor_id, envelope, text))

        # Cold 티어는 ColdSimulator(통계 일괄 처리)의 몫 — Phase 1은 대상 없음 (ADR-012)
        return decided

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
                # 대상 있는 행동은 관계 응고의 재료다 (CONSOLIDATE, ADR-016 규칙 1)
                if env["type"] == ACTION_TYPE and env["payload"].get("target_actor_id"):
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
        self._replies = []
        self._resolved_shifts = list(self._shifts)  # 관계 응고용 스냅샷 (ADR-016 규칙 2)
        self._shifts = []
        return emitted

    async def consolidate(self, ctx: TickContext) -> None:
        # 관계 응고 (ADR-016 §갱신 규칙 — CONSOLIDATE 단계)
        if self._relationship is not None:
            await self._consolidate_relationships(ctx)
        # 감정 감쇠·baseline 회귀 (ADR-015 §감쇠) — 기억 응고는 ADR-008 단계에서
        if self._emotion is not None:
            for persona in self._personas.values():
                await self._emotion.decay_one_tick(ctx.world_id, persona)

    async def _consolidate_relationships(self, ctx: TickContext) -> None:
        assert self._relationship is not None
        rel = self._relationship
        pending: list[PendingRelEvent] = []

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
            kind = f"action.{envelope['payload']['action_kind']}"
            pending += await rel.record_interaction(
                ctx.world_id, actor_id, target, kind, "outgoing", cause=envelope
            )
            pending += await rel.record_interaction(
                ctx.world_id, target, actor_id, kind, "incoming", cause=envelope
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

        # 규칙 4: 시간 감쇠 — 전 활성 엣지
        for actor_id in sorted(self._personas):
            pending += await rel.decay_all(ctx.world_id, actor_id)

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

        self._resolved_actions = []
        self._resolved_shifts = []
