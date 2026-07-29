"""tick 파이프라인의 Actor Runtime 구현 (ADR-011/012).

인지 루프의 Phase 절단면:
  perceive(메일박스 drain) → (appraise/emotion은 ADR-015 단계에서)
  → decide(행동 + 플레이어 응답) → act(RESOLVE 적재)
decide는 Context Fabric 조립 → AI Runtime 호출, 실패 시 규칙 폴백 —
액터는 '머뭇거린' 것으로 처리되고 tick은 멈추지 않는다.
플레이어 상호작용(댓글/DM)은 반드시 응답된다 (상호작용 우선, ADR-012 규칙 2).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from lf_eventstore import (
    DecisionTrace,
    NewEvent,
    Provenance,
    TracePolicy,
    append,
    current_head,
    store_trace,
)
from lf_relationship import STAGE_ACTION_KINDS
from lf_schemas import registry
from lf_tick.lod import (
    ActorLod,
    Tier,
    due_by_tier,
    is_due,
    maybe_demote,
    phase_offset,
    promote,
    scheduled_counts,
    touch,
)
from lf_tick.pipeline import TickContext
from redis.asyncio import Redis

from lf_actor.arc import Arc, ArcStore
from lf_actor.client import AiRuntimeClient, Inference
from lf_actor.cognition import CognitiveBudget, CognitiveBudgets
from lf_actor.consolidation import (
    Episode,
    ImportanceWeights,
    TickMaterials,
    build_episode,
    describe_interaction,
)
from lf_actor.context import Bundle, WorldContext, build
from lf_actor.conversation import conversation_turns
from lf_actor.emotion import PRINCIPAL as EMOTION_PRINCIPAL
from lf_actor.emotion import SHIFT_TYPE, EmotionAdapter, PendingShift
from lf_actor.goal import PRINCIPAL as GOAL_PRINCIPAL
from lf_actor.goal import GoalAdapter, PendingGoalEvent
from lf_actor.ledger import DecayLedger
from lf_actor.mailbox import Mailbox
from lf_actor.memo import (
    action_memo,
    belief_memo,
    proactive_dm_memo,
    reply_to_actor_memo,
    reply_to_player_memo,
    spontaneous_comment_memo,
)
from lf_actor.memory import WorkingMemory
from lf_actor.outreach import OutreachLedger, pick_dm_target
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
from lf_actor.resonance import GIST_MAX, Resonance, ResonanceStore
from lf_actor.rhythm import default_params, posting_moment
from lf_actor.rules import (
    fallback_action,
    fallback_follow_up,
    fallback_greeting,
    fallback_reply,
    routine_action,
)
from lf_actor.semantic import Recollection, SemanticMemory
from lf_actor.social import extract_comment, with_comment_path

logger = logging.getLogger("lf.actor.phases")

PRINCIPAL = "engine.actor"
ACTION_TYPE = "actor.action.performed"
#: 결정의 입력 기록 — ADR-009 §3이 약속한 이벤트 (ADR-021 §2)
DECISION_TYPE = "actor.decision.made"
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

#: 플레이어 댓글 — 여운(드라마 재생산, plan/02)이 남을 수 있는 유일한 교환.
#: 공개 대화라 그 사슬의 후속 포스트가 '이 대화가 낳은 이야기'로 성립한다
COMMENT_TYPE = "player.comment.posted"

#: 플레이어의 반응(좋아요) — 응답 의무는 없지만, 엣지 없는 플레이어의 '첫' 좋아요만은
#: 가벼운 인사 댓글을 받는다 (plan/03 §첫 개입 — 무반응으로 끝나면 루프는 시작도 못 한다)
REACTION_TYPE = "player.reaction.added"

#: Director의 사적 지목 (nudge_perception) — 반응을 기대하는 관측이다 (ADR-013)
OBSERVATION_TYPE = "world.observation.surfaced"
INCIDENT_TYPE = "world.incident.occurred"
#: 이웃의 피드 글 — 라우터 피드 순환이 배달한다 (액터 소셜 루프). LOD는 touch만
FEED_TYPE = "feed.post.published"
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

    Hot 승격 신호 셋: 응답 의무가 있는 플레이어 상호작용(dm/comment — 상호작용
    우선, ADR-012 규칙 2), Director의 사적 지목(nudge 관측 — 반응을 기대하고
    심은 지각이라 잠든 채 두면 다음 due까지 썩는다, ADR-013), 고강도 세계
    사건(내 삶을 흔든 사건은 곧바로 반응하게 한다 — 임계 high_intensity).

    **액터 댓글은 승격하지 않는다** — 답글 의무 경로는 due 무관이라 Hot이
    필요 없고, 댓글→promote는 자기 강화 루프였다: 포스트→댓글→작성자 Hot→
    매 tick 행동→포스트… 전원이 상시 Hot이 되어 tick이 LLM 큐에 눌렸다
    (2026-07-17 라이브 관측). 그 밖의 지각(반응·액터 댓글·저강도 사건·이웃의
    피드 글)은 관심 신호 — 티어는 유지하고 강등 타이머만 리셋한다(touch).
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


def _action_provenance(payload: dict[str, Any]) -> Provenance:
    """확정 행동의 출처 — decision_trace가 이미 두 경로를 갈라 놓았다 (ADR-012).

    cold_rule은 Cold 티어의 통계 행동이거나 LLM 실패 폴백이라 결정적 규칙 파생이고,
    나머지는 LLM이 이 인물답게 지어낸 의도다. tier가 cold_rule이 아닌데 trace_id가
    없으면 ValueError로 터진다 — 생성물을 규칙으로 잘못 부르느니 시끄럽게 실패한다
    (payload 스키마도 decision_trace.trace_id를 필수로 요구하므로 정상 경로엔 없다).
    """
    trace = payload.get("decision_trace") or {}
    if trace.get("tier") == "cold_rule":
        return Provenance.derived(f"actor.rules:{_rule_name(payload)}")
    return Provenance.generated(trace.get("trace_id") or "")


def _rule_name(payload: dict[str, Any]) -> str:
    """어느 규칙이 이 행동을 만들었나 — payload가 이미 알고 있다.

    셋 다 tier=cold_rule이라 티어만으로는 갈리지 않는다. rule_id는 L2 재실행의
    진입점이므로(ADR-021 §1) 뭉뚱그리면 검증기가 다른 규칙을 돌려 놓고
    '어긋났다'고 보고한다 — 없는 회귀를 만드는 셈이다.
    """
    params = payload.get("params") or {}
    if params.get("routine"):
        return "routine_action"
    if params.get("resonance"):
        return "fallback_follow_up"
    return "fallback_action"


def _episode_provenance(episode: Episode) -> Provenance:
    """응고된 기억의 출처 — 근거 사건이 있으면 인출이다 (ADR-021 §1).

    답장만으로 만들어진 tick은 근거가 비어 있다(build_episode는 replies를
    source_ids에 넣지 않는다). 그때 recalled이라 부르면 근거 없는 기억 주장이
    되므로, 규칙 파생으로 남기고 rule_id에 그 사정을 적어 데이터에서 보이게 한다.
    """
    if episode.source_event_ids:
        return Provenance.recalled(episode.source_event_ids)
    return Provenance.derived("memory.consolidate:replies_only")


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
        outreach: OutreachLedger | None = None,
        resonance: ResonanceStore | None = None,
        shard_select: Callable[[Persona], bool] | None = None,
        hot_start_cap: int | None = None,
        hot_floor: int = 0,
        decide_concurrency: int = 8,
        trace_policy: TracePolicy | None = None,
        cognition: CognitiveBudgets | None = None,
    ) -> None:
        if not personas:
            raise ValueError("액터가 없다 — 최소 1명의 페르소나가 필요하다")
        #: 세계 전체 명부(id→이름) — 이름 그라운딩·유효 대상·관계 서술의 원천.
        #: 샤드 분할(ADR-012 Phase 2)과 무관하게 언제나 전체다: 타 샤드 액터도
        #: 말 걸 수 있는 실존 인물이고, 화면엔 실명이 나가야 한다
        self._roster: dict[str, str] = {p.id: p.name for p in personas}
        #: 실행 집합 — 이 워커(샤드)가 tick마다 움직이는 액터들. 솔로는 전체
        self._shard_select = shard_select
        self._personas = {
            p.id: p for p in personas if shard_select is None or shard_select(p)
        }
        self._ai = ai
        # decide의 LLM 호출은 액터 간 독립이라 tick 안에서 병렬로 낸다 (직렬 await는
        # AI Runtime의 유계 동시성을 놀린다 — 매 호출 왕복만큼 tick 예산을 태운다).
        # 엔진 쪽 상한으로 팬아웃을 조인다: AI Runtime 슬롯(LF_AI_CONCURRENCY=4 기본)을
        # 꽉 채우되, NATS request의 10s 타임아웃을 태우는 깊은 대기열은 만들지 않는다
        # (기본 8 = 4 처리 + 4 대기). 봉투 순서는 gather가 제출 순서로 돌려줘 보존된다.
        self._decide_sem = asyncio.Semaphore(max(1, decide_concurrency))
        #: 결정 원문의 보존 정책 (ADR-021 §5) — 기본은 1% 샘플링·7일.
        #: 정책이 무엇이든 actor.decision.made 이벤트 자체는 언제나 남는다
        #: (L1 재조립 보증의 근거라 샘플링 대상이 아니다).
        self._trace_policy = trace_policy or TracePolicy()
        #: 인지 예산 해석기 (ADR-021 §3) — 오버라이드가 없으면 현행 동작 그대로.
        #: 실험 설정이 걸린 세계는 대조군이 아니므로 기동 로그에 남긴다.
        self._cognition = cognition or CognitiveBudgets.from_params(default_params())
        if self._cognition.has_overrides:
            logger.info("인지 예산 오버라이드 적용 — 이 세계는 실험 설정이다 (ADR-021 §3)")
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
        # 상시 Hot 바닥 (활기 모드) — 앞의 N명(id순)은 유휴여도 강등되지 않아 계속
        # ambient 활동을 만든다 (perceive의 강등 분기에서 면제). 0이면 유휴 저전력:
        # 모든 액터가 유휴 시 Cold로 강등되어 개입할 때만 깨어난다 (GPU 최소, 기본).
        self._hot_floor_ids = (
            set(sorted(self._personas)[: max(0, hot_floor)]) if hot_floor else set()
        )
        # 초기 티어 — 전원 Hot 시작은 대규모 세계에서 tick당 LLM 폭주다 (GPU·비용).
        # hot_start_cap이 서면 앞의 N명(id순)만 Hot으로 시작하고 나머지는 Warm(10 tick
        # 케이던스, 유휴 시 Cold로 강등)이다. 플레이어 개입·Director 지목은 여전히 즉시
        # Hot 승격이라 세계는 살아 있다. None이면 전원 Hot (소규모·테스트 하위 호환).
        # 바닥(floor) 액터는 항상 Hot으로 시작한다 (start_cap보다 커도 보장).
        start_ids = (
            set(sorted(self._personas)[:hot_start_cap])
            if hot_start_cap is not None
            else set(self._personas)
        )
        hot_ids = start_ids | self._hot_floor_ids
        self._lods: dict[str, ActorLod] = {
            actor_id: ActorLod(
                tier=Tier.HOT if actor_id in hot_ids else Tier.WARM,
                last_interest_tick=0,
            )
            for actor_id in self._personas
        }
        #: (actor_id, tier, payload[, causation_id, correlation_id]) — 여운 분출
        #: intent만 5-튜플로 원 대화의 인과·사슬을 실어 온다 (resolve가 관대하게 푼다)
        self._intents: list[tuple[Any, ...]] = []
        #: 이번 tick에 응답할 상호작용: (actor_id, 원인 봉투, 답장 텍스트)
        self._replies: list[tuple[str, dict[str, Any], str, Provenance]] = []
        #: 이번 tick의 결정 기록 — actor_id → payload들 (ADR-021 §2).
        #: RESOLVE에서 그 액터의 스트림에 함께 적재된다.
        self._decisions: dict[str, list[dict[str, Any]]] = {}
        #: 이번 tick에 이 액터가 쓴 추론 호출 수 — calls_per_tick의 집행 계수
        self._calls_this_tick: dict[str, int] = {}
        #: 선제 DM 빈도 장부 — 없으면 선제 DM 경로 자체가 없다 (후방 호환)
        self._outreach = outreach
        #: 여운 저장소 (드라마 재생산, plan/02) — 없으면 경로 자체가 없다 (후방 호환)
        self._resonance = resonance
        #: perceive가 찾은 첫 접촉 좋아요: (actor_id, reaction 봉투) — decide가 인사한다.
        #: 엣지 존재는 perceive 시점(이번 tick의 관계 응고 이전) 기준이다 (plan/03)
        self._pending_greetings: list[tuple[str, dict[str, Any]]] = []
        #: 이번 tick의 선제 DM: (actor_id, player_id, 텍스트) — RESOLVE 적재 (plan/02)
        self._proactive_dms: list[tuple[str, str, str, Provenance]] = []
        #: perceive가 채우는 tick당 수신함
        self._inbox: dict[str, list[dict[str, Any]]] = {}
        #: 피드에서 본 이웃의 글 — 다음 LLM decide까지 들고 간다 (액터 소셜 루프).
        #: 그 decide에서 소진된다 — 댓글 기회는 한 번뿐이다 (상한: seen_posts_max)
        self._seen_posts: dict[str, list[dict[str, Any]]] = {}
        #: 이번 tick의 자발 댓글: (actor_id, 대상 포스트 봉투, 텍스트) — RESOLVE 적재
        self._pending_comments: list[tuple[str, dict[str, Any], str, Provenance]] = []
        #: 액터별 마지막 자발 댓글 tick — 쿨다운(social.comment_cooldown_ticks)의
        #: 장부. in-memory — 재시작 시 쿨다운을 잊는 건 허용 오차다
        self._last_comment: dict[str, int] = {}
        #: perceive의 감정 평가 결과 — RESOLVE에서 engine.emotion으로 적재 (ADR-015)
        self._shifts: list[PendingShift] = []
        #: RESOLVE가 남기는 이번 tick의 응고 재료 — CONSOLIDATE의 관계·기억 입력 (ADR-008/016)
        self._resolved_actions: list[tuple[str, dict[str, Any]]] = []  # 대상 있는 행동 (관계용)
        self._resolved_action_all: list[tuple[str, dict[str, Any]]] = []  # 전 행동 (목표용)
        #: 적재 확정된 액터→액터 메시지 (규칙 1c 송신측 재료) — 유실분은 관계가 아니다
        self._resolved_messages: list[tuple[str, dict[str, Any]]] = []
        self._resolved_shifts: list[PendingShift] = []
        self._resolved_replies: list[tuple[str, dict[str, Any], str, Provenance]] = []
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

    def refresh_personas(
        self, personas: list[Persona], *, tick: int
    ) -> tuple[list[str], list[str]]:
        """리로드된 명부를 tick 경계에서 반영한다 (페르소나 스튜디오 핫 리로드).

        - 새 액터: 현재 tick 기준 Hot — 등장 직후 첫 decide가 바로 나온다 (데뷔).
          정체성 선언은 다음 world()의 몫이다 (SETNX가 재선언을 막는다 — 세계당 1회).
        - 사라진(휴면) 액터: 명부·LOD·진행 중 자료구조에서 제거 — 남는 참조는
          .get 기반이라 결손 키에 관대하다. 역사·관계는 이벤트에 남는다.
        - 필드 변경: Persona 객체 교체 — 다음 decide부터 새 결.
        반환: (추가된 id들, 제거된 id들) — 호출자의 로그 한 줄용.
        """
        if not personas:
            raise ValueError("액터가 없다 — 최소 1명의 페르소나가 필요하다")
        # 로스터는 전체, 실행 집합은 샤드 필터 후 (ADR-012 Phase 2) — 타 샤드의
        # 신인도 이름·유효 대상으로는 곧장 실존한다
        self._roster = {p.id: p.name for p in personas}
        incoming = {
            p.id: p
            for p in personas
            if self._shard_select is None or self._shard_select(p)
        }
        added = sorted(set(incoming) - set(self._personas))
        removed = sorted(set(self._personas) - set(incoming))
        self._personas = incoming
        for actor_id in added:
            self._lods[actor_id] = ActorLod(tier=Tier.HOT, last_interest_tick=tick)
        for actor_id in removed:
            self._lods.pop(actor_id, None)
            self._inbox.pop(actor_id, None)
            self._seen_posts.pop(actor_id, None)
            self._last_decay.pop(actor_id, None)
            self._ledger_dirty.discard(actor_id)
            # 재등장 시 world()가 Redis SETNX를 다시 확인한다 — 재선언은 없다
            self._declared.discard(actor_id)
        return added, removed

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
                    # 성격·목표·비밀은 전부 저작물이다 — 스튜디오면 빚은 사람,
                    # 시드 페르소나면 저장소의 YAML이 저자다 (ADR-021 §1)
                    provenance=Provenance.authored(persona.created_by or "system:seed"),
                    payload={
                        "name": persona.name,
                        "archetype": persona.archetype or "unknown",
                        "bio": bio or persona.name,
                        "goals": goals,
                        # 저자성 — 스튜디오 태생이면 빚은 플레이어, 시스템 태생은 null.
                        # '당신이 빚은 인물' 데뷔 표식의 원천이다 (페르소나 스튜디오)
                        "created_by": persona.created_by,
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
        self._pending_greetings = []
        if not self._ledger_loaded:
            await self._hydrate_ledger(ctx.world_id)
        # 전 액터 수신함을 한 파이프라인으로 비운다 — 액터 수만큼 왕복하던 drain을
        # 1왕복으로 (유휴 세계의 빈 수신함 순회 비용 제거, ADR-012)
        actor_ids = list(self._personas)
        drained = (
            await self._mailbox.drain_many(ctx.world_id, actor_ids) if self._mailbox else {}
        )
        for actor_id in actor_ids:
            items = drained.get(actor_id, [])
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
            # LOD 갱신: 승격이 최우선(Hot), 없으면 지각 규칙, 지각도 없으면 유휴 강등
            # (ADR-011). 이웃의 피드 글 배달은 '나를 향한 관심'이 아니라 소비 입력이라
            # 관심 신호에서 제외한다 — 활발한 세계에선 몇 tick마다 배달이 와서 touch만
            #으로 전원이 상시 Hot을 유지했다 (2026-07-18 과열 관측: 강등이 영영 안 옴)
            attention = [e for e in items if e["type"] != FEED_TYPE]
            if promoted:
                self._lods[actor_id] = promote(self._lods[actor_id], ctx.tick)
            elif attention:
                self._lods[actor_id] = lod_after_perception(
                    self._lods[actor_id], attention, ctx.tick,
                    high_intensity=self._promote_intensity,
                )
            elif actor_id in self._hot_floor_ids:
                # 활기 모드의 상시 Hot 바닥 — 유휴여도 강등하지 않고 Hot을 유지한다
                # (ambient 활동 지속, GPU↑). 유휴 저전력 모드(floor=0)에선 이 분기가 없다.
                self._lods[actor_id] = promote(self._lods[actor_id], ctx.tick)
            else:
                self._lods[actor_id] = maybe_demote(self._lods[actor_id], ctx.tick)
            if promoted or items:
                # 잠들었던 액터가 깨어난다 — 밀린 감쇠를 상태 사용 전에 정산 (ADR-012)
                await self._catch_up(ctx, actor_id)
            if not items:
                continue  # 승격 신호는 기억·감정·목표에 들어가지 않는다 (메타 제어)
            self._inbox[actor_id] = items
            names = dict(self._roster)
            for envelope in items:
                await self._memory.add(
                    ctx.world_id, actor_id, describe_interaction(envelope, names)
                )
            # 첫 접촉 판정 — 엣지 없는 플레이어의 첫 좋아요만 인사 대기열에 (plan/03).
            # 이번 tick의 관계 응고(CONSOLIDATE가 reaction으로 엣지를 만든다) 이전의
            # perceive 시점 상태가 기준이다 — 어댑터 없으면 판정 자체가 불가능해 침묵
            if self._relationship is not None:
                await self._note_first_reactions(ctx.world_id, actor_id, items)
            # 본 글은 다음 LLM decide까지 들고 간다 — 자발 댓글의 재료 (소셜 루프)
            if (feed_items := [e for e in items if e["type"] == FEED_TYPE]):
                seen = self._seen_posts.setdefault(actor_id, [])
                seen += feed_items
                keep = int(default_params()["social"]["seen_posts_max"])
                del seen[:-keep]  # 오래된 것부터 밀려난다
            if self._emotion is not None:
                shifts, mood_line = await self._emotion.appraise(
                    ctx.world_id, self._personas[actor_id], items, tick=ctx.tick,
                    edges=await self._post_edges(ctx.world_id, actor_id, items),
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

    async def _note_first_reactions(
        self, world_id: str, actor_id: str, items: list[dict[str, Any]]
    ) -> None:
        """엣지 없는 플레이어의 첫 좋아요 → 인사 대기열 (액터당 tick당 상한, plan/03).

        이후 좋아요(엣지 있음)는 기존 그대로다 — 내부 감정·관계 입력일 뿐 무반응.
        상한 초과분도 침묵하지만 CONSOLIDATE가 엣지는 만든다 (첫 접촉은 한 번뿐이다).
        """
        assert self._relationship is not None
        cap = int(default_params()["outreach"]["greeting_max_per_tick"])
        greeted: set[str] = set()
        for envelope in items:
            if envelope["type"] != REACTION_TYPE or len(greeted) >= cap:
                continue
            player_id = envelope["payload"]["player_id"]
            if player_id in greeted:
                continue
            if await self._relationship.load(world_id, actor_id, player_id) is None:
                self._pending_greetings.append((actor_id, envelope))
                greeted.add(player_id)

    async def decide(self, ctx: TickContext) -> dict[str, int]:
        self._intents = []
        self._replies = []
        self._decisions = {}
        self._calls_this_tick = {}
        self._pending_comments = []
        self._proactive_dms = []
        decided = {"hot": 0, "warm": 0, "cold": 0}
        due = due_by_tier(self._lods, ctx.tick)
        world = WorldContext(world_id=ctx.world_id, tick=ctx.tick, world_time=ctx.world_time)
        schema = registry.payload_schema(ACTION_TYPE)

        # Hot/Warm 행동 — 액터 간 독립이라 tick 안에서 유계 병렬로 낸다. asyncio.gather는
        # 제출 순서로 결과를 돌려주므로 intent 순서는 직렬 때와 동일하다 (결정성 보존).
        due_actors = [
            (actor_id, tier) for tier in (Tier.HOT, Tier.WARM) for actor_id in due[tier]
        ]

        async def _decide_action(actor_id: str, tier: Tier) -> dict[str, Any]:
            payload, trace_id = await self._llm_action(
                ctx, world, actor_id, schema, tier=tier.value
            )
            if payload is None:
                payload = fallback_action(self._personas[actor_id], ctx.tick, trace_id)
            # LLM이 지어낸 대상을 소스에서 끊는다 — 피드·관계·그래프로 번지기 전에
            return sanitize_target(payload, set(self._roster), actor_id)

        for (actor_id, tier), payload in zip(
            due_actors,
            await asyncio.gather(
                *(self._bounded(_decide_action(a, t)) for a, t in due_actors)
            ),
            strict=True,
        ):
            self._intents.append((actor_id, tier.value, payload))
            decided[tier.value] += 1

        # 플레이어·댓글 응답 + 첫 접촉 인사 — 모두 self._replies로 모인다. 반드시
        # 응답(상호작용 우선)이라 병렬로 내되, gather 제출 순서로 다시 담아 한 액터가
        # 여러 봉투를 받았을 때의 답장 순서까지 직렬 때와 동일하게 보존한다.
        reply_coros = []
        for actor_id in sorted(self._inbox):
            for envelope in self._inbox[actor_id]:
                if envelope["type"] == MESSAGE_TYPE:
                    # 내 글에 달린 액터 댓글 — 응답 의무와 동형, 반드시 한 번 답한다
                    # (액터 소셜 루프). 답글은 라우터가 더 돌리지 않는다 (깊이 1)
                    reply_coros.append(self._reply_to_comment(ctx, world, actor_id, envelope))
                elif envelope["type"] in _REPLYABLE:
                    reply_coros.append(self._reply_to_player(ctx, world, actor_id, envelope))
        # 첫 접촉 인사 — 엣지 없는 플레이어의 첫 좋아요가 무반응으로 끝나지 않게
        # (plan/03 §첫 개입). 표현은 LLM(converse), 보증은 규칙 템플릿 풀이다.
        for actor_id, envelope in self._pending_greetings:
            reply_coros.append(self._greet_first_reaction(ctx, world, actor_id, envelope))
        for reply in await asyncio.gather(*(self._bounded(c) for c in reply_coros)):
            if reply is not None:
                self._replies.append(reply)

        # Cold 티어 — 통계 일괄 처리(ADR-012): LLM 없이 일과 행동만, due일 때만(100 tick
        # 케이던스). 아크 조회(Redis)만 병렬로 앞당기고 나머지는 순수 계산이다.
        cold_actors = list(due[Tier.COLD])
        for actor_id, arc in zip(
            cold_actors,
            # 아크(있으면)가 일과의 결이 된다 — 잠든 삶도 방향이 있다 (ADR-013/plan-08)
            await asyncio.gather(*(self._arc_of(ctx.world_id, a) for a in cold_actors)),
            strict=True,
        ):
            persona = self._personas[actor_id]
            payload = routine_action(persona, ctx.tick, f"cold-{actor_id}-{ctx.tick}", arc=arc)
            payload = sanitize_target(payload, set(self._roster), actor_id)
            self._intents.append((actor_id, Tier.COLD.value, payload))
            decided["cold"] += 1

        # SNS 포스팅 리듬 — due가 아니어도 생활 패턴상 '근황을 남기고 싶은 순간'인
        # 액터는 LLM decide를 돈다. 표현이 목적이므로 AI 실패는 규칙 문장으로 채우지
        # 않고 조용히 생략한다. 자기 발화는 관심 신호가 아니다 — LOD 불변 (ADR-011).
        already = {actor_id for actors in due.values() for actor_id in actors}
        rhythm = default_params()

        # 여운 분출 (드라마 재생산, plan/02) — 리듬 모먼트에서, LOD 불문. non-due
        # 한정을 걸면 강렬한 교환으로 Hot이 된 액터 — 즉 여운이 가장 잘 남는
        # 액터 — 일수록 분출이 영영 못 나가는 역설이 생긴다 (선제 DM의 교훈).
        # due 액터는 행동과 분출이 한 tick에 공존하고, 아니면 분출이 리듬 포스팅을
        # 대체한다. 빈도의 빗장은 여운의 희소성(임계)·1회 소모·쿨다운·TTL이다.
        # burst 집합은 아래 리듬 포스팅의 제외 대상이라 리듬보다 먼저 확정한다(장벽).
        burst: set[str] = set()
        if self._resonance is not None:
            burst_actors = [
                a for a in sorted(self._personas)
                if posting_moment(a, self._personas[a].lifestyle, ctx.world_time, rhythm)
            ]
            for a, did in zip(
                burst_actors,
                await asyncio.gather(
                    *(self._bounded(self._burst_resonance(ctx, world, a, schema))
                      for a in burst_actors)
                ),
                strict=True,
            ):
                if did:
                    burst.add(a)
                    # tick.completed의 actors_decided는 hot/warm/cold로 닫힌 스키마 —
                    # 리듬 관례를 따라 warm에 합산한다
                    decided["warm"] += 1

        rhythm_actors = [
            a for a in sorted(set(self._personas) - already - burst)
            if posting_moment(a, self._personas[a].lifestyle, ctx.world_time, rhythm)
        ]
        for actor_id, (payload, _) in zip(
            rhythm_actors,
            await asyncio.gather(
                *(self._bounded(
                    self._llm_action(
                        ctx, world, a, schema, tier=Tier.WARM.value, purpose="post_status"
                    )
                ) for a in rhythm_actors)
            ),
            strict=True,
        ):
            if payload is None:
                continue  # 머뭇거림 — 포스팅 모먼트는 지나가면 그만이다
            payload = sanitize_target(payload, set(self._roster), actor_id)
            self._intents.append((actor_id, "rhythm", payload))
            # tick.completed의 actors_decided는 hot/warm/cold로 닫힌 스키마다
            # (packages/schemas) — 리듬 분은 warm에 합산하고 로그로만 구분한다
            decided["warm"] += 1
            logger.info("리듬 결정: %s tick=%d (근황 모먼트 — warm 합산)", actor_id, ctx.tick)

        # '기억됨' 선제 DM (plan/02) — 리듬과 독립된 저빈도 기회 창. 리듬 모먼트에
        # 묶으면 Hot(리듬 제외)인 액터일수록 — 즉 관계가 활발할수록 — 안부가 영영
        # 못 나가는 역설이 생긴다 (2026-07-17 라이브 관측). 진짜 빗장은 쿨다운
        # (세계 하루 1회)이고, 기회 창은 지연 상한일 뿐이다. LOD 불문 검사한다.
        interval = max(1, int(default_params()["outreach"]["dm_check_ticks"]))
        dm_actors = [
            a for a in sorted(self._personas)
            if ctx.tick % interval == phase_offset(a, interval)
        ]
        await asyncio.gather(
            *(self._bounded(self._proactive_dm(ctx, world, a)) for a in dm_actors)
        )
        return decided

    async def _bounded(self, coro: Any) -> Any:
        """decide의 LLM/조회 코루틴을 엔진 쪽 동시성 상한 안에서 실행한다.

        gather로 팬아웃한 코루틴을 세마포어로 조여 AI Runtime의 유계 슬롯을
        꽉 채우되(놀리지 않고), NATS request 타임아웃을 태우는 깊은 대기열은
        만들지 않는다 (self._decide_sem 주석 참고).
        """
        async with self._decide_sem:
            return await coro

    async def _reply_to_player(
        self, ctx: TickContext, world: WorldContext, actor_id: str, envelope: dict[str, Any]
    ) -> tuple[str, dict[str, Any], str, Provenance]:
        """플레이어의 댓글/DM에 답한다 — 반환한 튜플을 호출자가 순서대로 담는다.

        답장도 결정이다 — 인생 방향·관계의 온도·이 사람과의 대화 흐름이 결을
        물들인다 (ADR-009/013). 표현은 LLM(converse), 실패 시 규칙 폴백.
        """
        persona = self._personas[actor_id]
        working = await self._memory.recent(ctx.world_id, actor_id)
        episodes = await self._recall(ctx.world_id, actor_id, working)
        # 이 플레이어와의 대화를 시간순으로 — 답장이 흐름을 잇게 한다 (ADR-009)
        conversation = conversation_turns(working, envelope["payload"]["player_id"])
        arc = await self._arc_of(ctx.world_id, actor_id)
        relationships = await self._relationship_summary(ctx.world_id, actor_id)
        bundle = build(
            persona, working, world, purpose="reply_to_player",
            episodes=episodes, conversation=conversation, arc=arc,
            relationships=relationships,
            memory_tokens=self._budget_for(actor_id).memory_tokens,
        )
        res = await self._ai_converse(ctx, actor_id, bundle, tier="hot")
        text = res.value
        if text is None:
            text = fallback_reply(persona, envelope["payload"]["text"])
            provenance = Provenance.derived("actor.rules:fallback_reply")
        else:
            provenance = Provenance.generated(bundle.trace_id)
        await self._record_decision(
            ctx, actor_id, bundle, purpose=bundle.purpose, tier="hot",
            outcome="acted" if res else "fallback", model=res.model,
            sampling=res.sampling, output=res.value,
        )
        return (actor_id, envelope, text, provenance)

    async def _reply_to_comment(
        self, ctx: TickContext, world: WorldContext, actor_id: str, envelope: dict[str, Any]
    ) -> tuple[str, dict[str, Any], str, Provenance]:
        """내 글에 달린 액터 댓글에 답한다 — 표현은 LLM, 보증은 규칙 폴백.

        플레이어 응답 의무와 동형이다 (반드시 한 번). 답글 이벤트의 in_reply_to는
        댓글 event_id — post_id와 달라 라우터의 순환 기준에서 벗어난다 (깊이 1 끝).
        반환한 튜플을 호출자가 순서대로 self._replies에 담는다.
        """
        persona = self._personas[actor_id]
        working = await self._memory.recent(ctx.world_id, actor_id)
        arc = await self._arc_of(ctx.world_id, actor_id)
        relationships = await self._relationship_summary(ctx.world_id, actor_id)
        bundle = build(
            persona, working, world, purpose="reply_to_comment",
            episodes=await self._recall(ctx.world_id, actor_id, working),
            arc=arc, relationships=relationships,
            memory_tokens=self._budget_for(actor_id).memory_tokens,
        )
        res = await self._ai_converse(ctx, actor_id, bundle, tier="hot")
        text = res.value
        if text is None:
            text = fallback_reply(persona, envelope["payload"]["text"])
            provenance = Provenance.derived("actor.rules:fallback_reply")
        else:
            provenance = Provenance.generated(bundle.trace_id)
        await self._record_decision(
            ctx, actor_id, bundle, purpose=bundle.purpose, tier="hot",
            outcome="acted" if res else "fallback", model=res.model,
            sampling=res.sampling, output=res.value,
        )
        return (actor_id, envelope, text, provenance)

    async def _greet_first_reaction(
        self, ctx: TickContext, world: WorldContext, actor_id: str, envelope: dict[str, Any]
    ) -> tuple[str, dict[str, Any], str, Provenance]:
        """첫 접촉 좋아요에 가벼운 인사 한 줄 — 그 포스트의 댓글로 (plan/03 §첫 개입).

        표현은 LLM(converse, 가벼운 첫 인사 결의 Task Frame), 실패 시 결정적 규칙
        템플릿 풀 — 첫 개입의 가시적 반응은 보증이다 (응답 의무와 같은 급).
        반환한 튜플을 호출자가 순서대로 self._replies에 담는다.
        """
        persona = self._personas[actor_id]
        working = await self._memory.recent(ctx.world_id, actor_id)
        bundle = build(
            persona, working, world, purpose="greet_reaction",
            relationships=await self._relationship_summary(ctx.world_id, actor_id),
            memory_tokens=self._budget_for(actor_id).memory_tokens,
        )
        res = await self._ai_converse(ctx, actor_id, bundle, tier="warm")
        text = res.value
        if text is None:
            text = fallback_greeting(persona, envelope["payload"]["player_id"])
            provenance = Provenance.derived("actor.rules:fallback_greeting")
        else:
            provenance = Provenance.generated(bundle.trace_id)
        await self._record_decision(
            ctx, actor_id, bundle, purpose=bundle.purpose, tier="warm",
            outcome="acted" if res else "fallback", model=res.model,
            sampling=res.sampling, output=res.value,
        )
        logger.info(
            "첫 접촉 인사: %s → %s tick=%d (plan/03 첫 개입)",
            actor_id, envelope["payload"]["player_id"], ctx.tick,
        )
        return (actor_id, envelope, text, provenance)

    async def _proactive_dm(
        self, ctx: TickContext, world: WorldContext, actor_id: str
    ) -> bool:
        """'기억됨' 선제 DM — 임계를 넘은 최고 관계 플레이어 1명에게 먼저 안부 (plan/02).

        보수적이다: 자기 리듬 모먼트에만, (액터,플레이어)당 세계 하루(360 tick)
        1회 이하(Redis 장부 — 재시작에 견고). 표현이 목적이라 AI 실패는 조용히
        생략한다(False — 규칙 문장으로 선제 안부를 보내지 않고, 호출자의 리듬
        포스팅 경로가 이어진다). True면 DM이 이 모먼트의 포스팅을 대체한다.
        """
        if self._relationship is None or self._outreach is None:
            return False
        knobs = default_params()["outreach"]
        edges = {}
        for other in await self._relationship.counterparts(ctx.world_id, actor_id):
            state = await self._relationship.load(ctx.world_id, actor_id, other)
            if state is not None:
                edges[other] = state
        target = pick_dm_target(edges, threshold=float(knobs["dm_threshold"]))
        if target is None:
            return False
        last = await self._outreach.last_sent_tick(ctx.world_id, actor_id, target)
        if last is not None and ctx.tick - last < int(knobs["dm_cooldown_ticks"]):
            return False
        # 함께 나눈 기억을 곁들인다 — 회상·작업기억·이 사람과의 대화 흐름 (ADR-008/009)
        persona = self._personas[actor_id]
        working = await self._memory.recent(ctx.world_id, actor_id)
        bundle = build(
            persona, working, world, purpose="proactive_dm",
            episodes=await self._recall(ctx.world_id, actor_id, working),
            conversation=conversation_turns(working, target),
            arc=await self._arc_of(ctx.world_id, actor_id),
            relationships=await self._relationship_summary(ctx.world_id, actor_id),
            memory_tokens=self._budget_for(actor_id).memory_tokens,
        )
        res = await self._ai_converse(ctx, actor_id, bundle, tier="warm")
        text = res.value
        await self._record_decision(
            ctx, actor_id, bundle, purpose=bundle.purpose, tier="warm",
            outcome="acted" if res else "hesitated", model=res.model,
            sampling=res.sampling, output=res.value,
        )
        if text is None:
            return False  # 조용히 생략 — 쿨다운도 소모하지 않는다 (다음 모먼트에 다시)
        await self._outreach.mark(ctx.world_id, actor_id, target, ctx.tick)
        self._proactive_dms.append(
            (actor_id, target, text, Provenance.generated(bundle.trace_id))
        )
        logger.info("선제 DM: %s → %s tick=%d ('기억됨', plan/02)", actor_id, target, ctx.tick)
        return True

    async def _burst_resonance(
        self, ctx: TickContext, world: WorldContext, actor_id: str, schema: dict[str, Any]
    ) -> bool:
        """여운 → 원 대화의 correlation을 승계한 후속 포스트 (드라마 재생산, plan/02).

        승계가 배지('이 대화가 낳은 이야기')와 loop_health 드라마 재생산율의 판정
        그 자체다 — 그래서 분출은 보증이다: 표현은 LLM, 실패 시 규칙 폴백이 문장을
        채운다 (조용한 생략 금지 — 리듬 포스팅과 다른 급). 과열 가드 4중: 액터당
        여운 1개(store 계약), correlation당 1회 소모(spend), 쿨다운(액터당), TTL
        (바랜 여운은 지우고 분출하지 않는다). 반환: 분출 여부.
        """
        assert self._resonance is not None
        res = await self._resonance.load(ctx.world_id, actor_id)
        if res is None:
            return False
        knobs = default_params()["resonance"]
        if ctx.tick - res.tick > int(knobs["ttl_ticks"]):
            await self._resonance.clear(ctx.world_id, actor_id)  # 바랜 여운 — 침묵
            return False
        last = await self._resonance.last_burst_tick(ctx.world_id, actor_id)
        if last is not None and ctx.tick - last < int(knobs["cooldown_ticks"]):
            return False
        persona = self._personas[actor_id]
        working = await self._memory.recent(ctx.world_id, actor_id)
        # 여운 요지가 재료다 — 상대는 세계 안 어휘('그 사람')로만 (리시트 정화의 결)
        material = (
            f'마음에 남은 대화: 그 사람이 남긴 말 "{res.comment}"'
            f' — 나는 "{res.reply}"라고 답했었다'
        )
        bundle = build(
            persona, [material, *working], world, purpose="follow_up_post",
            episodes=await self._recall(ctx.world_id, actor_id, working),
            arc=await self._arc_of(ctx.world_id, actor_id),
            relationships=await self._relationship_summary(ctx.world_id, actor_id),
            memory_tokens=self._budget_for(actor_id).memory_tokens,
        )
        # res는 여운(Resonance)이다 — 추론 결과는 따로 받는다 (이름이 겹치면 조용히 샌다)
        inference = await self._ai_decide(ctx, actor_id, bundle, schema, tier=Tier.WARM.value)
        payload = inference.value
        await self._record_decision(
            ctx, actor_id, bundle, purpose=bundle.purpose, tier=Tier.WARM.value,
            outcome="acted" if inference else "fallback", model=inference.model,
            sampling=inference.sampling,
            output=json.dumps(payload, ensure_ascii=False) if payload else None,
        )
        if payload is None:
            payload = fallback_follow_up(
                persona, ctx.tick, bundle.trace_id, fragment=res.comment
            )
        payload = sanitize_target(payload, set(self._roster), actor_id)
        # 5-튜플 — 원 대화의 인과·사슬을 RESOLVE까지 실어 간다 (승계의 배선 지점)
        self._intents.append(
            (actor_id, "rhythm", payload, res.source_event_id, res.correlation_id)
        )
        await self._resonance.spend(ctx.world_id, actor_id, res.correlation_id, ctx.tick)
        logger.info(
            "여운 분출: %s corr=%s tick=%d (드라마 재생산, plan/02)",
            actor_id, res.correlation_id, ctx.tick,
        )
        return True

    async def _record_resonances(self, ctx: TickContext) -> None:
        """플레이어 댓글 교환의 여운 기록 — 감정 강도가 임계 이상일 때만 (plan/02).

        강도의 원천은 이 tick의 감정 평가(perceive appraise)가 그 댓글에 남긴
        shift 인스턴스다 — 감정 어댑터가 없으면 여운도 없다 (감정 없는 교환은
        마음에 남지 않는다). DM·좋아요는 제외 — 여운은 공개 대화(댓글)의 것이다.
        저장 계약(액터당 1개·소모된 사슬 재기록 거부)은 ResonanceStore의 몫이다.
        """
        assert self._resonance is not None
        threshold = float(default_params()["resonance"]["threshold"])
        peak_by_cause: dict[str, float] = {}
        for shift in self._shifts:
            if shift.causation_id is None:
                continue
            intensity = float(shift.instance.get("intensity", 0.0))
            peak_by_cause[shift.causation_id] = max(
                peak_by_cause.get(shift.causation_id, 0.0), intensity
            )
        for actor_id, envelope, text, _ in self._replies:
            if envelope["type"] != COMMENT_TYPE:
                continue
            intensity = peak_by_cause.get(envelope["event_id"], 0.0)
            if intensity < threshold:
                continue
            recorded = await self._resonance.record(
                ctx.world_id, actor_id,
                Resonance(
                    correlation_id=envelope["correlation_id"],
                    source_event_id=envelope["event_id"],
                    player_id=envelope["payload"]["player_id"],
                    comment=envelope["payload"]["text"][:GIST_MAX],
                    reply=text[:GIST_MAX],
                    intensity=intensity,
                    tick=ctx.tick,
                ),
            )
            if recorded:
                logger.info(
                    "여운 기록: %s corr=%s intensity=%.2f tick=%d (plan/02)",
                    actor_id, envelope["correlation_id"], intensity, ctx.tick,
                )

    async def _llm_action(
        self,
        ctx: TickContext,
        world: WorldContext,
        actor_id: str,
        schema: dict[str, Any],
        *,
        tier: str,
        purpose: str = "decide_action",
    ) -> tuple[dict[str, Any] | None, str]:
        """컨텍스트 조립 → LLM 행동 결정. 반환: (payload 또는 None, trace_id).

        실패(None) 처리는 호출자의 몫이다 — due 결정은 규칙 폴백, 리듬은 생략.
        피드에서 본 글이 있으면 스키마에 선택적 comment 경로를 열고 컨텍스트에
        글을 놓는다 — 댓글 여부는 LLM의 몫, 규칙 폴백은 댓글을 만들지 않는다.
        """
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
        seen = self._seen_posts.get(actor_id, [])
        # 댓글 쿨다운 — 방금 댓글 단 액터는 기회 창을 닫는다 (본 글은 남아 다음
        # 기회에). 댓글마다 작성자의 답장(converse)이 따라오므로, 이 창이 곧
        # 수다의 예산이다 (2026-07-17 과열 관측: tick당 댓글 15건 × 답장 15건)
        social = default_params()["social"]
        last = self._last_comment.get(actor_id)
        if seen and last is not None and ctx.tick - last < int(
            social["comment_cooldown_ticks"]
        ):
            seen = []
        if seen:
            schema = with_comment_path(schema, seen)
        bundle = build(
            persona, working, world, purpose=purpose,
            episodes=episodes, arc=arc, relationships=relationships,
            # (post_id, 표시줄) — 본 글은 사건이라 결정의 근거로 기록된다 (ADR-021 §2)
            seen_posts=[(e["event_id"], self._post_line(e)) for e in seen] or None,
            memory_tokens=self._budget_for(actor_id).memory_tokens,
        )
        res = await self._ai_decide(ctx, actor_id, bundle, schema, tier=tier)
        payload = res.value
        await self._record_decision(
            ctx, actor_id, bundle, purpose=bundle.purpose, tier=tier,
            outcome="acted" if res else "hesitated", model=res.model,
            sampling=res.sampling,
            output=json.dumps(payload, ensure_ascii=False) if payload else None,
        )
        if payload is None:
            return None, bundle.trace_id  # 머뭇거림 — 본 글은 다음 decide로 넘어간다
        if seen:
            payload, comment = extract_comment(payload, seen)
            self._seen_posts.pop(actor_id, None)  # 댓글 기회는 이 결정에 소진됐다
            cap = int(social["max_comments_per_tick"])
            written = sum(1 for a, _, _, _ in self._pending_comments if a == actor_id)
            if comment is not None and written < cap:
                post, text = comment
                self._pending_comments.append(
                    (actor_id, post, text, Provenance.generated(bundle.trace_id))
                )
                self._last_comment[actor_id] = ctx.tick
        return payload, bundle.trace_id

    def _post_line(self, envelope: dict[str, Any]) -> str:
        """본 글 한 줄 — [post_id] 작성자: "제목" — 본문 일부 (comment 경로의 좌표)."""
        author = envelope.get("actor_id") or "?"
        name = self._roster.get(author, author)
        p = envelope["payload"]
        return f"[{envelope['event_id']}] {name}: \"{p['title']}\" — {p['body'][:100]}"

    async def _post_edges(
        self, world_id: str, actor_id: str, items: list[dict[str, Any]]
    ) -> dict[str, dict[str, float]]:
        """피드 글 작성자별 관계 차원 — appraise_post의 관계 입력 (액터 소셜 루프).

        엣지가 없는 작성자는 담지 않는다 — 모르는 사람의 글은 마음을 흔들지 않는다.
        """
        if self._relationship is None:
            return {}
        edges: dict[str, dict[str, float]] = {}
        for envelope in items:
            if envelope["type"] != FEED_TYPE:
                continue
            author = envelope.get("actor_id")
            if not author or author in edges:
                continue
            state = await self._relationship.load(world_id, actor_id, author)
            if state is not None:
                edges[author] = state.dimensions
        return edges

    async def _ai_converse(
        self, ctx: TickContext, actor_id: str, bundle: Bundle, *, tier: str
    ) -> Inference:
        """예산을 쓰고 converse — 슬롯이 없으면 부르지 않는다 (ADR-021 §3)."""
        if not self._spend_call(actor_id):
            logger.info("인지 예산 소진: %s tick=%d — %s를 규칙으로",
                        actor_id, ctx.tick, bundle.purpose)
            return Inference()
        return await self._ai.converse(bundle, tier=tier, actor_id=actor_id, tick=ctx.tick)

    async def _ai_decide(
        self, ctx: TickContext, actor_id: str, bundle: Bundle,
        schema: dict[str, Any], *, tier: str,
    ) -> Inference:
        """예산을 쓰고 decide_action — 슬롯이 없으면 부르지 않는다."""
        if not self._spend_call(actor_id):
            logger.info("인지 예산 소진: %s tick=%d — %s를 규칙으로",
                        actor_id, ctx.tick, bundle.purpose)
            return Inference()
        return await self._ai.decide_action(
            bundle, schema, tier=tier, actor_id=actor_id, tick=ctx.tick
        )

    async def _ai_reflect(
        self, ctx: TickContext, actor_id: str, bundle: Bundle, schema: dict[str, Any]
    ) -> Inference:
        """예산을 쓰고 reflect — 슬롯이 없으면 곱씹지 않는다 (규칙 신념이 바닥을 지킨다)."""
        if not self._spend_call(actor_id):
            logger.info("인지 예산 소진: %s tick=%d — 곱씹음 생략", actor_id, ctx.tick)
            return Inference()
        return await self._ai.reflect(bundle, schema, actor_id=actor_id, tick=ctx.tick)

    def _spend_call(self, actor_id: str) -> bool:
        """이번 tick의 추론 슬롯을 하나 쓴다 — 남아 있지 않으면 False.

        소진의 결과는 세계 예산과 같다: 규칙 폴백이며, 그 폴백이
        provenance=derived 이벤트로 남아 화면에서도 데이터에서도 구분된다
        (ADR-012/018 §4, ADR-021 §1/§3). 조용한 성공 위장은 없다.
        """
        used = self._calls_this_tick.get(actor_id, 0)
        if used >= self._budget_for(actor_id).calls_per_tick:
            return False
        self._calls_this_tick[actor_id] = used + 1
        return True

    def _budget_for(self, actor_id: str) -> CognitiveBudget:
        """이 인물의 인지 예산 — 티어 기본 위에 오버라이드가 얹힌다."""
        lod = self._lods.get(actor_id)
        tier = lod.tier.value if lod is not None else Tier.WARM.value
        return self._cognition.for_actor(actor_id, tier)

    async def _record_decision(
        self,
        ctx: TickContext,
        actor_id: str,
        bundle: Bundle,
        *,
        purpose: str,
        tier: str,
        outcome: str,
        model: str | None,
        sampling: dict[str, Any] | None = None,
        output: str | None = None,
    ) -> None:
        """결정의 입력을 기록한다 — "무엇을 보고 그렇게 했는가" (ADR-021 §2).

        원문은 정책이 허락할 때만 별도 저장소에 남고(§5), 섹션 구조와 digest는
        언제나 이벤트로 남는다 — 그것이 L1 재조립 보증의 근거다. 트레이스 저장이
        실패해도 결정 기록은 남긴다: 관측이 시뮬레이션을 멈춰서는 안 된다.
        """
        try:
            retained = await store_trace(
                ctx.conn,
                DecisionTrace(
                    trace_id=bundle.trace_id,
                    world_id=ctx.world_id,
                    actor_id=actor_id,
                    tick=ctx.tick,
                    purpose=purpose,
                    system_prompt=bundle.system,
                    user_prompt=bundle.user,
                    output=output,
                    model=model,
                ),
                self._trace_policy,
            )
        except Exception as e:  # noqa: BLE001 — 관측 실패가 tick을 멈추면 안 된다
            logger.warning("결정 트레이스 저장 실패(기록은 계속): %s", e)
            retained = False

        self._decisions.setdefault(actor_id, []).append(
            {
                "trace_id": bundle.trace_id,
                "purpose": purpose,
                "tier": tier,
                "sections": [
                    {
                        "kind": section.kind,
                        "source_ids": list(section.source_ids),
                        "token_count": section.token_count,
                    }
                    for section in bundle.sections
                ],
                "bundle_digest": bundle.digest,
                "model": model,
                # 샘플링 파라미터는 AI Runtime이 아직 응답에 싣지 않는다 —
                # 스키마는 null을 허용하며, 에코가 붙는 단계에서 채워진다 (ADR-018).
                # 프로바이더에 실제로 보낸 값 (ADR-021 §2). null 필드는 "지정하지
                # 않음 = 프로바이더 기본값"이며 규칙 경로는 통째로 None이다.
                "sampling": sampling,
                # 어떤 인지 예산으로 내린 결정인지 (ADR-021 §3) — 실험 결과를
                # 예산과 이어 읽으려면 결정마다 그 값이 함께 있어야 한다
                "cognitive_budget": self._budget_for(actor_id).to_json(),
                "outcome": outcome,
                "trace_retained": retained,
            }
        )

    async def _recall(
        self, world_id: str, actor_id: str, working: list[str]
    ) -> list[Recollection]:
        """장기 기억 회상 — 최신 지각을 질의로 유사 에피소드 top-k (ADR-008).

        슬롯 수는 이 인물의 인지 예산이 정한다 (ADR-021 §3) — 기본값은 지금까지
        쓰던 3이라, 오버라이드가 없으면 회상의 폭이 달라지지 않는다.
        """
        if self._semantic is None or not working:
            return []
        slots = self._budget_for(actor_id).recall_slots
        return await self._semantic.recall(world_id, actor_id, working[0], k=slots)

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
        names = dict(self._roster)
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
        self, ctx: TickContext, actor_id: str, source: dict[str, Any], text: str,
        provenance: Provenance,
    ) -> NewEvent:
        if source["type"] == MESSAGE_TYPE:
            # 액터 댓글에의 답글 — in_reply_to가 댓글 event_id라 순환 기준(post_id와
            # 동일)에서 벗어난다: 답글은 다시 의무를 만들지 않는다 (깊이 1 종결)
            return NewEvent(
                world_id=ctx.world_id,
                stream="actor",
                stream_key=actor_id,
                type=MESSAGE_TYPE,
                tick=ctx.tick,
                actor_id=actor_id,
                causation_id=source["event_id"],
                correlation_id=source["correlation_id"],  # 글이 시작한 사슬을 잇는다
                provenance=provenance,
                payload={
                    "channel": "comment",
                    "target_player_id": None,
                    "target_actor_id": source["actor_id"],  # 댓글 작성자에게
                    "text": text[:1000],
                    "post_id": source["payload"]["post_id"],
                    "in_reply_to": source["event_id"],
                },
            )
        if source["type"] == REACTION_TYPE:
            # 첫 접촉 인사 — 좋아요가 달린 그 포스트의 댓글로 (plan/03 §첫 개입).
            # in_reply_to는 reaction event_id(= causation) — post_id와 달라 라우터의
            # 순환 기준에서 벗어나고, 수신자도 플레이어라 액터 배달은 없다 (깊이 0 종결)
            return NewEvent(
                world_id=ctx.world_id,
                stream="actor",
                stream_key=actor_id,
                type=MESSAGE_TYPE,
                tick=ctx.tick,
                actor_id=actor_id,
                causation_id=source["event_id"],
                # 첫 좋아요가 시작한 사슬을 잇는다 — '당신이 시작한 이야기' (ADR-013)
                correlation_id=source["correlation_id"],
                provenance=provenance,
                payload={
                    "channel": "comment",
                    "target_player_id": source["payload"]["player_id"],
                    "text": text[:1000],
                    "post_id": source["payload"]["post_id"],
                    "in_reply_to": source["event_id"],
                },
            )
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
            provenance=provenance,
            payload={
                "channel": channel,
                "target_player_id": source["payload"]["player_id"],
                "text": text[:1000],
                "post_id": source["payload"].get("post_id"),
                "in_reply_to": source["event_id"],
            },
        )

    def _comment_event(
        self, ctx: TickContext, actor_id: str, post: dict[str, Any], text: str,
        provenance: Provenance,
    ) -> NewEvent:
        """이웃의 글에 남기는 자발 댓글 (액터 소셜 루프).

        in_reply_to == post_id — 최상위 댓글의 표식이자 라우터가 글 작성자에게
        배달하는 근거다 (social.comment_targets, 깊이 1 차단).
        """
        return NewEvent(
            world_id=ctx.world_id,
            stream="actor",
            stream_key=actor_id,
            type=MESSAGE_TYPE,
            tick=ctx.tick,
            actor_id=actor_id,
            causation_id=post["event_id"],
            correlation_id=post["correlation_id"],  # 글이 시작한 서사 사슬을 잇는다
            provenance=provenance,
            payload={
                "channel": "comment",
                "target_player_id": None,
                "target_actor_id": post["actor_id"],
                "text": text[:1000],
                "post_id": post["event_id"],
                "in_reply_to": post["event_id"],
            },
        )

    async def resolve(self, ctx: TickContext) -> int:
        """충돌 해소 → 확정 이벤트 적재. actor_id 순 순차·결정적 (ADR-011 §4).

        같은 액터의 응답과 행동은 한 번의 append(단일 스트림 CAS)로 묶인다 —
        응답이 행동보다 앞선다 (상호작용 우선, ADR-012 규칙 2).
        """
        self._resolved_actions = []
        self._resolved_action_all = []
        self._resolved_messages = []
        events_by_actor: dict[str, list[NewEvent]] = {}
        memos: dict[str, list[str]] = {}

        # 결정 기록이 먼저다 — 같은 스트림에서 stream_seq가 순서를 정하므로,
        # 결정이 그 결과보다 앞에 놓여야 인과가 읽는 순서와 어긋나지 않는다.
        # 출처는 derived다: 이 이벤트는 조립기가 남긴 사실이지 LLM의 해석이 아니다
        # (그 안에 실린 model·outcome이 LLM 쪽을 가리킬 뿐이다, ADR-021 §1).
        for actor_id in sorted(self._decisions):
            for payload in self._decisions[actor_id]:
                events_by_actor.setdefault(actor_id, []).append(
                    NewEvent(
                        world_id=ctx.world_id,
                        stream="actor",
                        stream_key=actor_id,
                        type=DECISION_TYPE,
                        tick=ctx.tick,
                        actor_id=actor_id,
                        provenance=Provenance.derived(
                            f"context.assemble:{payload['purpose']}"
                        ),
                        payload=payload,
                    )
                )

        for actor_id, envelope, text, provenance in self._replies:
            events_by_actor.setdefault(actor_id, []).append(
                self._reply_event(ctx, actor_id, envelope, text, provenance)
            )
            if envelope["type"] == MESSAGE_TYPE:
                commenter = envelope["actor_id"]
                who = self._roster.get(commenter, commenter)
                memo = reply_to_actor_memo(ctx.tick, who, text)
            else:
                player = envelope["payload"]["player_id"]
                memo = reply_to_player_memo(ctx.tick, player, text)
            memos.setdefault(actor_id, []).append(memo)

        # 여운 기록 (드라마 재생산, plan/02) — 강렬했던 플레이어 댓글 교환은
        # 마음에 남아, 이후 리듬 모먼트에 원 사슬을 승계한 후속 포스트가 된다
        if self._resonance is not None:
            await self._record_resonances(ctx)

        # 선제 DM ('기억됨', plan/02) — 원인 플레이어 이벤트가 없는 자발 발화라
        # causation 없음·in_reply_to null (스키마가 허용, 사슬의 시작이 된다)
        for actor_id, player_id, text, provenance in self._proactive_dms:
            events_by_actor.setdefault(actor_id, []).append(
                NewEvent(
                    world_id=ctx.world_id,
                    stream="actor",
                    stream_key=actor_id,
                    type=MESSAGE_TYPE,
                    tick=ctx.tick,
                    actor_id=actor_id,
                    provenance=provenance,
                    payload={
                        "channel": "dm",
                        "target_player_id": player_id,
                        "text": text[:1000],
                        "post_id": None,
                        "in_reply_to": None,
                    },
                )
            )
            memos.setdefault(actor_id, []).append(
                proactive_dm_memo(ctx.tick, player_id, text)
            )

        # 자발 댓글 (액터 소셜 루프) — 응답 뒤·행동 앞: 사회적 반응이 일과보다 앞선다
        for actor_id, post, text, provenance in self._pending_comments:
            events_by_actor.setdefault(actor_id, []).append(
                self._comment_event(ctx, actor_id, post, text, provenance)
            )
            author = post["actor_id"]
            name = self._roster.get(author, author)
            memos.setdefault(actor_id, []).append(
                spontaneous_comment_memo(ctx.tick, name, text)
            )

        for intent in self._intents:
            actor_id, payload = intent[0], intent[2]
            # 여운 분출 intent(5-튜플)만 원 대화의 인과·사슬을 실어 온다 — 이 승계가
            # '이 대화가 낳은 이야기' 배지와 드라마 재생산율의 판정이다 (plan/02).
            # 나머지(3-튜플)는 사슬 없음 — append가 새 사슬의 루트로 만든다 (ADR-002)
            causation_id = intent[3] if len(intent) > 3 else None
            correlation_id = intent[4] if len(intent) > 4 else None
            events_by_actor.setdefault(actor_id, []).append(
                NewEvent(
                    world_id=ctx.world_id,
                    stream="actor",
                    stream_key=actor_id,
                    type=ACTION_TYPE,
                    tick=ctx.tick,
                    actor_id=actor_id,
                    causation_id=causation_id,
                    correlation_id=correlation_id,
                    provenance=_action_provenance(payload),
                    payload=payload,
                )
            )
            memos.setdefault(actor_id, []).append(action_memo(ctx.tick, payload))

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
                            provenance=Provenance.derived("emotion.appraise"),
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
                elif env["type"] == MESSAGE_TYPE:
                    # 액터에게 건넨 말(댓글·답글)도 관계 응고의 재료다 (규칙 1c 송신측).
                    # 적재 확정 봉투만 모은다 — 유실된 말이 관계가 되면 안 된다.
                    # 플레이어 대상 응답·선제 DM(target_player_id)은 이 규칙 밖이다
                    target = env["payload"].get("target_actor_id")
                    if target and target != actor_id:
                        self._resolved_messages.append((actor_id, env))
            # 결정 기록만 있고 산출이 없는 액터도 있다(머뭇거림) — 남길 메모가 없다.
            # 작업 기억은 '무엇을 했나'의 기록이라, 안 한 일을 적으면 다음 tick의
            # 컨텍스트가 오염된다 (ADR-021 §결과 "관측이 시뮬레이션을 바꿀 위험").
            for memo in memos.get(actor_id, ()):
                # 자기 행동/응답 → Working Memory 유입 (지각의 최소 형태, ADR-008)
                await self._memory.add(ctx.world_id, actor_id, memo)
            logger.info(
                "확정: %s tick=%d 이벤트 %d건 (응답 %d) head=%d",
                actor_id, ctx.tick, len(stored),
                sum(1 for e in events if e.type == MESSAGE_TYPE),
                stored[-1].stream_seq,
            )

        self._intents = []
        self._pending_comments = []
        self._proactive_dms = []
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
                    provenance=Provenance.derived("goal.engine"),
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
                        provenance=Provenance.derived("emotion.appraise:goal"),
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
        names = dict(self._roster)
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
            # 규칙 신념은 감정·관계 '상태'에서 결정적으로 파생된다 — 사건 인출이
            # 아니라 recalled이 아니고, 지어낸 해석도 아니라 generated가 아니다
            pairs: list[tuple[Belief, Provenance]] = [
                (b, Provenance.derived(f"reflection:{b.kind}")) for b in beliefs
            ]
            insight = await self._llm_insight(ctx, actor_id, set(edges), names)
            if insight is not None:
                pairs.append(insight)
            for belief, provenance in pairs:
                if not await self._ledger.changed(ctx.world_id, actor_id, belief):
                    continue
                await self._store_belief(ctx, actor_id, belief, provenance)
                # 인물 통찰은 관계 비중에 스민다 — 그 사람이 마음에서 자리를 차지한다
                # (ADR-016, 엣지가 있을 때만 — 생각만으로 관계가 시작되진 않는다)
                if belief.kind == "person_insight" and belief.about_id:
                    await self._relationship.record_insight(
                        ctx.world_id, actor_id, belief.about_id, belief.confidence
                    )

    async def _llm_insight(
        self, ctx: TickContext, actor_id: str, counterparts: set[str], names: dict[str, str]
    ) -> tuple[Belief, Provenance] | None:
        """작업 기억을 곱씹은 LLM 통찰 하나 — "이 경험들이 의미하는 것" (ADR-008).

        대상 후보는 아는 사람(관계 상대 + 동료 액터)뿐 — 환각 대상은 insight_to_belief가
        끊는다. 곱씹을 경험이 없으면 묻지 않는다 (빈 기억에서 통찰은 안 나온다).
        """
        working = await self._memory.recent(ctx.world_id, actor_id)
        if not working:
            return None
        known = sorted(counterparts | (set(self._roster) - {actor_id}))
        roster = ", ".join(f"{names.get(a, a)}({a})" for a in known) or "(없음)"
        world = WorldContext(world_id=ctx.world_id, tick=ctx.tick, world_time=ctx.world_time)
        bundle = build(
            self._personas[actor_id], [f"아는 사람들: {roster}", *working],
            world, purpose="reflect",
            memory_tokens=self._budget_for(actor_id).memory_tokens,
        )
        res = await self._ai_reflect(ctx, actor_id, bundle, insight_schema(known))
        output = res.value
        await self._record_decision(
            ctx, actor_id, bundle, purpose=bundle.purpose, tier="warm",
            outcome="acted" if res else "hesitated", model=res.model,
            sampling=res.sampling,
            output=json.dumps(output, ensure_ascii=False) if output else None,
        )
        if output is None:
            return None
        belief = insight_to_belief(output, set(known))
        if belief is None:
            return None
        logger.info("LLM 통찰: %s [%s] conf=%.2f", actor_id, belief.kind, belief.confidence)
        return belief, Provenance.generated(bundle.trace_id)

    async def _store_belief(
        self, ctx: TickContext, actor_id: str, belief: Belief, provenance: Provenance
    ) -> None:
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
                    provenance=provenance,
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
        await self._memory.add(ctx.world_id, actor_id, belief_memo(belief.statement))
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
        names = dict(self._roster)
        peaks: dict[str, float] = {}
        for shift in self._resolved_shifts:
            intensity = float(shift.instance.get("intensity", 0.0))
            peaks[shift.actor_id] = max(peaks.get(shift.actor_id, 0.0), intensity)

        for actor_id in sorted(self._personas):
            materials = TickMaterials(
                interactions=self._inbox.get(actor_id, []),
                replies=[
                    (env, text) for a, env, text, _ in self._resolved_replies if a == actor_id
                ],
                action_envelope=actions_by_actor.get(actor_id),
                emotion_peak=peaks.get(actor_id, 0.0),
                relationship_events=rel_counts.get(actor_id, 0),
                goal_relevance=goal_relevance.get(actor_id, 0.0),
            )
            episode = build_episode(materials, weights=self._weights, names=names)
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
                    provenance=_episode_provenance(episode),
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
                if envelope["type"].startswith("player."):
                    pending += await rel.record_interaction(
                        ctx.world_id, actor_id, envelope["payload"]["player_id"],
                        envelope["type"], "incoming", cause=envelope,
                    )
                elif envelope["type"] == MESSAGE_TYPE:
                    # 규칙 1c(수신측): 오간 말도 관계다 — 이웃의 댓글이 내게 닿았다.
                    # inbox의 actor.message.sent는 라우터가 나를 겨눠 배달한 최상위
                    # 댓글뿐이다 (comment_targets — target_actor_id=나, 깊이 1 차단).
                    # 소셜 루프의 가장 흔한 관계 행위인데 어느 규칙에도 안 타서
                    # 액터↔액터 간선이 0이었다 (2026-07-18 실측 공백)
                    sender = envelope.get("actor_id")
                    if sender and sender != actor_id:
                        pending += await rel.record_interaction(
                            ctx.world_id, actor_id, sender,
                            envelope["type"], "incoming", cause=envelope,
                        )

        # 규칙 1c(송신측): 적재 확정된 액터→액터 메시지 → 발신자→수신자 엣지.
        # 수신자 쪽 엣지는 배달·지각(다음 tick inbox의 수신측 1c)이 만든다 —
        # 지각 전의 말은 상대의 마음을 못 바꾼다 (규칙 1b의 즉시 양방향과 다른 결).
        # 델타 크기·성장 속도 어림은 lf_relationship params.yaml 주석이 원천이다
        for actor_id, envelope in self._resolved_messages:
            pending += await rel.record_interaction(
                ctx.world_id, actor_id, envelope["payload"]["target_actor_id"],
                envelope["type"], "outgoing", cause=envelope,
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
                    provenance=Provenance.derived("relationship.engine"),
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
