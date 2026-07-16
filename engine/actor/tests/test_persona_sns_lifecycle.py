"""신규 캐릭터의 SNS 생활 E2E 시나리오 — 생성→페르소나→세계 입장→생활 검증.

테스트 계획 (가상 AI 캐릭터의 SNS 생활 체크리스트):
  1. 캐릭터 생성  — 런타임에 페르소나를 조립한다 (파일·특정 인물 하드코딩 없음)
  2. 세계 입장    — actor.identity.declared 로 이름·소개·목표가 세계에 노출된다
  3. 자율 활동    — tick마다 성격·욕구에 맞는 행동(actor.action.performed)을 남긴다
  4. 피드 노출    — 행동이 편집 임계를 넘으면 feed.post.published 로 승격된다
  5. 사회적 응답  — 플레이어 DM에 반드시 답한다 (actor.message.sent, 상호작용 우선)
  6. 관계 형성    — 첫 개입이 first_met 마일스톤과 관계 엣지를 만든다
  7. 감정 반응    — 개입이 감정(actor.emotion.shifted)으로 남는다
  8. 내면 축적    — tick의 재료가 기억(actor.memory.consolidated)으로 응고된다
  9. 곱씹음       — reflection tick에 상태 패턴이 신념(actor.belief.formed)이 된다

PG+Redis(+NATS AI Runtime rule 프로바이더) 필요 — conftest 게이트.
LLM 없이도(규칙 폴백) 전 사이클이 살아있어야 한다는 보증이 이 테스트의 핵심이다.
"""

from datetime import UTC, datetime
from pathlib import Path

from lf_actor.arc import ArcStore
from lf_actor.client import AiRuntimeClient
from lf_actor.emotion import EmotionAdapter
from lf_actor.goal import GoalAdapter
from lf_actor.mailbox import Mailbox
from lf_actor.memory import WorkingMemory
from lf_actor.persona import Persona
from lf_actor.phases import ActorPhases
from lf_actor.reflection import BeliefLedger
from lf_actor.relationship import RelationshipAdapter
from lf_eventstore import new_ulid, read_stream
from lf_feed.composer import FeedComposer
from lf_feed.config import Config as FeedConfig
from lf_feed.scoring import ScoringConfig
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick
from lf_tick.lod import COLD_INTERVAL, ActorLod, Tier, phase_offset

from .test_phases import ai_service, nc  # noqa: F401 — 픽스처 재사용

WORLD = "w_lifecycle"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))
NEWCOMER = "a_probe_newcomer"


def make_newcomer() -> Persona:
    """1. 캐릭터 생성 — 테스트가 즉석에서 조립하는 일반 신규 캐릭터.

    특정 실존/데모 인물이 아니라 '방금 세계에 들어온 누군가'의 최소 스펙이다.
    achievement 편향 → 규칙 경로에서도 work 중심의 SNS 생활이 나온다.
    """
    return Persona(
        id=NEWCOMER,
        name="새내기",
        archetype="probe_newcomer",
        identity_core="방금 이 세계에 들어온 신입 — 자기 자리를 만들려 애쓰는 중이다.",
        big_five={"openness": 0.6, "conscientiousness": 0.7, "extraversion": 0.5,
                  "agreeableness": 0.6, "neuroticism": 0.5},
        needs_bias={"achievement": 0.8, "belonging": 0.6, "security": 0.4},
        goals=({"id": "g_first_step", "description": "첫 결과물을 세상에 내놓기",
                "priority": 0.8, "need": "achievement"},),
    )


def make_companion() -> Persona:
    """두 번째 신규 캐릭터 — 소속 편향 (다수 투입 시나리오의 상대역)."""
    return Persona(
        id="a_probe_companion",
        name="길동무",
        archetype="probe_companion",
        identity_core="같은 날 들어온 또 한 사람 — 곁을 먼저 살피는 성격이다.",
        big_five={"openness": 0.5, "conscientiousness": 0.5, "extraversion": 0.7,
                  "agreeableness": 0.8, "neuroticism": 0.4},
        needs_bias={"achievement": 0.4, "belonging": 0.9, "security": 0.5},
        goals=({"id": "g_find_people", "description": "마음 붙일 사람들 찾기",
                "priority": 0.8, "need": "belonging"},),
    )


class _ScriptedAi:
    """대인 행동을 고르는 LLM을 재현하는 스텁 — 서로에게 말을 걸게 한다.

    규칙 폴백은 대상 있는 행동을 만들지 않으므로, LLM이 골랐을 speak를
    결정적으로 재현한다. resolve→CONSOLIDATE(관계·기억·피드)는 실제 경로다.
    """

    def __init__(self, targets: dict[str, str]) -> None:
        self._targets = targets

    async def decide_action(self, bundle, schema, *, tier, actor_id, tick):
        return {
            "action_kind": "speak",
            "intent": "같은 날 들어온 동료에게 먼저 인사를 건넨다",
            "target_actor_id": self._targets.get(actor_id),
            "location_id": None,
            "params": {},
            "decision_trace": {"trace_id": f"scripted-{actor_id}-{tick}", "tier": tier},
        }

    async def converse(self, *args, **kwargs):
        return None

    async def reflect(self, *args, **kwargs):
        return None


async def test_multiple_new_characters_form_a_society(conn, redis):
    """다수 신규 캐릭터 동시 투입 — 서로의 존재가 관계·피드·기억에 남는다.

    테스트 계획 확장: 캐릭터들이 혼자가 아니라 '사회'로 사는지 — 상호 관계
    엣지, 첫 만남 마일스톤, 대인 행동의 피드 승격, 서로에 대한 기억.
    """
    a, b = make_newcomer(), make_companion()
    phases = ActorPhases(
        [a, b],
        ai=_ScriptedAi({a.id: b.id, b.id: a.id}),  # 서로에게 말을 건다
        memory=WorkingMemory(redis),
        emotion=EmotionAdapter(redis),
        relationship=RelationshipAdapter(redis),
        identity_redis=redis,
    )
    head = 0
    for tick in range(2):
        head = await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=head)

    # 둘 다 세계에 입장했다
    for persona in (a, b):
        events = [s.envelope for s in await read_stream(conn, WORLD, "actor", persona.id)]
        assert any(e["type"] == "actor.identity.declared" for e in events)
        actions = [e for e in events if e["type"] == "actor.action.performed"]
        assert actions and all(act["payload"]["target_actor_id"] for act in actions)

    # 상호 관계 — 양방향 엣지와 첫 만남 마일스톤 (규칙 1b: 대상 있는 행동은 양방향)
    rel = RelationshipAdapter(redis)
    assert await rel.load(WORLD, a.id, b.id) is not None
    assert await rel.load(WORLD, b.id, a.id) is not None
    for pair in (f"{a.id}|{b.id}", f"{b.id}|{a.id}"):
        rel_events = [
            s.envelope for s in await read_stream(conn, WORLD, "relationship", pair)
        ]
        assert any(
            e["type"] == "relationship.milestone.reached"
            and e["payload"]["milestone"] == "first_met"
            for e in rel_events
        )

    # 대인 행동이 피드에 오른다 — 두 이름이 한 제목에
    composer = FeedComposer(
        FeedConfig(pg_dsn="unused", nats_url="unused", env="test",
                   personas_dir=Path("agents/personas-없음")),
        actor_names={a.id: a.name, b.id: b.name},
    )
    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", a.id)]
    first_action = next(e for e in events if e["type"] == "actor.action.performed")
    post_id = await composer.compose_once(conn, first_action)
    assert post_id is not None
    [post] = await read_stream(conn, WORLD, "feed", post_id)
    title = post.envelope["payload"]["title"]
    assert a.name in title and b.name in title  # "새내기, 길동무에게 말을 걸다"

    # 서로의 흔적이 기억에 남는다
    recent_a = await WorkingMemory(redis).recent(WORLD, a.id)
    assert any("인사를 건넨다" in m for m in recent_a)


def dm_envelope(text: str) -> dict:
    event_id = new_ulid()
    return {
        "event_id": event_id, "stream": "player", "type": "player.dm.sent",
        "schema_version": 1, "world_id": WORLD, "actor_id": None, "tick": 0,
        "occurred_at": "2026-03-01T00:00:00Z", "causation_id": None,
        "correlation_id": event_id,
        "payload": {"player_id": "p_watcher", "target_actor_id": NEWCOMER, "text": text},
    }


class _RecordingAi:
    """decide 컨텍스트를 기록만 하는 스텁 — 아크가 결정의 재료가 됐는지 관측한다."""

    def __init__(self) -> None:
        self.bundles: list[str] = []

    async def decide_action(self, bundle, schema, *, tier, actor_id, tick):
        self.bundles.append(bundle.user)
        return None  # 규칙 폴백 — 행동은 결정적으로, 관측은 컨텍스트로

    async def converse(self, *args, **kwargs):
        return None

    async def reflect(self, *args, **kwargs):
        return None


async def test_arc_shapes_a_characters_life(conn, redis):
    """테스트 계획 확장 — 아크 부여 후 행동의 결 변화 관측 (plan/08 전환점 사슬).

    Director의 arc_planned가 메일박스로 오면: (a) 기억을 오염시키지 않고 저장되고,
    (b) 다음 decide 컨텍스트의 최상위 프레임이 되며, (c) 아크가 미는 욕구의 목표가
    스포트라이트를 받고, (d) 잠든(Cold) 날의 일과 서술에도 인생 방향이 스민다.
    """
    persona = make_newcomer()  # 목표: 첫 결과물(achievement) — prime 아크가 민다
    ai = _RecordingAi()
    mailbox = Mailbox(redis)
    phases = ActorPhases(
        [persona],
        ai=ai,
        memory=WorkingMemory(redis),
        mailbox=mailbox,
        goal=GoalAdapter(redis),
        arc=ArcStore(redis),
    )
    intention = "이제는 증명의 시간 — 완성한 것을 세상에 내보인다"
    arc_event_id = new_ulid()
    await mailbox.push(WORLD, persona.id, {
        "event_id": arc_event_id, "stream": "system",
        "type": "system.director.arc_planned", "schema_version": 1,
        "world_id": WORLD, "actor_id": None, "tick": 0,
        "occurred_at": "2026-03-01T00:00:00Z", "causation_id": None,
        "correlation_id": arc_event_id,
        "payload": {"target_actor_id": persona.id, "stage": "prime",
                    "intention": intention},
    })
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    # (b) 아크가 결정의 최상위 프레임 — decide 컨텍스트에 들어갔다
    [bundle] = ai.bundles
    assert "## 인생 아크" in bundle
    assert "전성기·침체기" in bundle and intention in bundle
    # (c) prime이 미는 욕구(achievement)의 목표가 스포트라이트를 받는다
    assert "마음이 기우는 곳" in bundle
    assert bundle.index("## 인생 아크") < bundle.index("## 작업 기억")

    # (a) 제어 신호는 기억에 남지 않는다 — 액터는 연출을 눈치채지 못한다
    recent = await WorkingMemory(redis).recent(WORLD, persona.id)
    assert not any(intention in m for m in recent)

    # (d) 잠든 날의 일과에도 인생 방향이 스민다 (Cold routine, ADR-012/013)
    phases._lods[persona.id] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    due = phase_offset(persona.id, COLD_INTERVAL)
    due_tick = due if due > 0 else COLD_INTERVAL
    await run_tick(conn, phases, CLOCK, WORLD, tick=due_tick, head=head)
    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", persona.id)]
    routines = [
        e for e in events
        if e["type"] == "actor.action.performed" and e["payload"]["params"].get("routine")
    ]
    assert routines and intention in routines[-1]["payload"]["intent"]


async def test_new_character_lives_a_full_sns_life(conn, redis, nc, ai_service):  # noqa: F811
    persona = make_newcomer()
    mailbox = Mailbox(redis)
    rel = RelationshipAdapter(redis)
    phases = ActorPhases(
        [persona],
        ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis),
        mailbox=mailbox,
        emotion=EmotionAdapter(redis),
        relationship=rel,
        goal=GoalAdapter(redis),
        belief_ledger=BeliefLedger(redis),
        reflection_interval=3,
        identity_redis=redis,
    )

    # 5. 사회적 응답의 재료 — 첫 tick에 플레이어가 말을 건다
    await mailbox.push(WORLD, NEWCOMER, dm_envelope("처음 보는 얼굴이네요. 환영해요!"))

    head = 0
    for tick in range(4):  # reflection_interval(3)을 지나도록 세계를 돌린다
        head = await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=head)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", NEWCOMER)]
    by_type: dict[str, list[dict]] = {}
    for envelope in events:
        by_type.setdefault(envelope["type"], []).append(envelope)

    # 2. 세계 입장 — 정체성이 선언됐다 (이름·소개·목표가 read 모델의 재료가 된다)
    [identity] = by_type["actor.identity.declared"]
    assert identity["payload"]["name"] == persona.name
    assert identity["payload"]["goals"][0]["description"] == "첫 결과물을 세상에 내놓기"

    # 3. 자율 활동 — 매 tick 행동이 남았다 (LLM 없이도 규칙 폴백으로)
    actions = by_type["actor.action.performed"]
    assert len(actions) == 4
    assert all(a["payload"]["intent"] for a in actions)

    # 5. 사회적 응답 — DM에 반드시 답했다 (상호작용 우선, ADR-012 규칙 2)
    [reply] = by_type["actor.message.sent"]
    assert reply["payload"]["target_player_id"] == "p_watcher"
    assert reply["payload"]["text"]

    # 7. 감정 반응 — 환영 인사가 감정으로 남았다
    assert by_type["actor.emotion.shifted"]

    # 8. 내면 축적 — 그 날의 재료가 기억으로 응고됐다
    assert by_type["actor.memory.consolidated"]

    # 9. 곱씹음 — reflection tick(3)에 관계 상태가 신념이 됐다
    beliefs = by_type.get("actor.belief.formed", [])
    assert beliefs, "reflection tick에서 신념이 형성됐어야 한다"

    # 6. 관계 형성 — 플레이어와의 첫 만남이 마일스톤으로 남았다
    rel_events = [
        s.envelope
        for s in await read_stream(conn, WORLD, "relationship", f"{NEWCOMER}|p_watcher")
    ]
    assert any(
        e["type"] == "relationship.milestone.reached"
        and e["payload"]["milestone"] == "first_met"
        for e in rel_events
    )
    assert await rel.load(WORLD, NEWCOMER, "p_watcher") is not None  # 엣지가 살아있다

    # 4. 피드 노출 — 행동 하나가 편집 임계를 넘어 SNS 피드에 승격된다
    composer = FeedComposer(
        FeedConfig(pg_dsn="unused", nats_url="unused", env="test",
                   personas_dir=Path("agents/personas-없음"),
                   scoring=ScoringConfig(threshold=0.3)),
        actor_names={persona.id: persona.name},
    )
    promoted = [
        post_id for action in actions
        if (post_id := await composer.compose_once(conn, action)) is not None
    ]
    assert promoted, "행동이 하나도 피드에 오르지 못했다 — SNS 생활이 보이지 않는다"
    [first_post] = await read_stream(conn, WORLD, "feed", promoted[0])
    assert persona.name in first_post.envelope["payload"]["title"]

    # 그리고 이 모든 것이 작업 기억으로 이어진다 — 다음 결정의 컨텍스트
    recent = await WorkingMemory(redis).recent(WORLD, NEWCOMER)
    assert any("p_watcher" in m for m in recent)  # 개입의 흔적
    assert any("나는" in m for m in recent)  # 자기 행동의 흔적
