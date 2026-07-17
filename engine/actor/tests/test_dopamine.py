"""도파민 포인트 2종 — 첫 접촉 인사·'기억됨' 선제 DM (plan/02·03).

① 첫 접촉 인사: 관계 엣지가 없는 플레이어의 첫 좋아요는 가벼운 인사 댓글을
   받는다 — "첫 개입이 무반응으로 끝나면 루프는 시작도 못 한다" (plan/03).
   이후 좋아요(엣지 있음)는 기존 그대로 무반응이다.
② 선제 DM: 자기 리듬 모먼트 tick에 관계 임계(trust+intimacy)를 넘은 플레이어가
   있으면 먼저 안부 DM을 보낸다 — "신뢰를 얻은 AI가 먼저 DM을 보내는 순간이
   레벨업보다 강하다" (plan/02). 세계 하루 1회 이하, AI 실패는 조용히 생략.

프로브 페르소나 즉석 조립 — 특정 인물 하드코딩 없음 (사용자 표준).
PG+Redis 필요 (conftest 게이트).
"""

from datetime import UTC, datetime

from lf_actor.conversation import conversation_turns
from lf_actor.mailbox import Mailbox
from lf_actor.memory import WorkingMemory
from lf_actor.outreach import OutreachLedger, pick_dm_target
from lf_actor.persona import Persona
from lf_actor.phases import ActorPhases
from lf_actor.relationship import RelationshipAdapter
from lf_actor.rhythm import default_params, posting_moment
from lf_actor.rules import fallback_greeting
from lf_eventstore import new_ulid, read_stream
from lf_relationship import RelationshipState
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick
from lf_tick.lod import ActorLod, Tier, is_due

WORLD = "w_dopamine"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))
ACTOR = "a_probe_host"
PLAYER = "p_probe_fan"
TICKS_PER_DAY = 360


def make_host() -> Persona:
    return Persona(
        id=ACTOR,
        name="집주인",
        archetype="probe_host",
        identity_core="찾아오는 사람을 오래 기억하는 주민.",
        big_five={"openness": 0.6, "conscientiousness": 0.5, "extraversion": 0.6,
                  "agreeableness": 0.7, "neuroticism": 0.4},
        needs_bias={"belonging": 0.8, "achievement": 0.5, "security": 0.4},
        goals=({"id": "g_people", "description": "마음 붙일 사람들 찾기",
                "priority": 0.8, "need": "belonging"},),
        lifestyle="flexible",
    )


def like_envelope(player_id: str = PLAYER, *, tick: int = 0) -> dict:
    event_id = new_ulid()
    return {
        "event_id": event_id, "stream": "player", "type": "player.reaction.added",
        "schema_version": 1, "world_id": WORLD, "actor_id": None, "tick": tick,
        "occurred_at": "2026-03-01T00:00:00Z", "causation_id": None,
        "correlation_id": event_id,
        "payload": {"player_id": player_id, "target_actor_id": ACTOR,
                    "post_id": new_ulid(), "kind": "like"},
    }


def edge_state(trust: float, intimacy: float) -> RelationshipState:
    return RelationshipState(
        dimensions={"trust": trust, "intimacy": intimacy, "respect": 0.2,
                    "attraction": 0.0, "resentment": 0.0},
        stage="friend", salience=0.7,
    )


async def seed_edge(redis, from_id: str, to_id: str, state: RelationshipState) -> None:
    adapter = RelationshipAdapter(redis)
    await adapter.save(WORLD, from_id, to_id, state)
    await redis.sadd(f"lf:relidx:{WORLD}:{from_id}", to_id)


class _SilentAi:
    """LLM 없는 세계 — 규칙 폴백만 돈다 (dev 결정성)."""

    async def decide_action(self, bundle, schema, *, tier, actor_id, tick):
        return None

    async def converse(self, bundle, *, tier, actor_id, tick):
        return None

    async def reflect(self, *args, **kwargs):
        return None


class _ConverseAi(_SilentAi):
    """converse(인사·선제 DM)에 응답하는 스텁 — 컨텍스트를 기록한다."""

    def __init__(self, text: str | None) -> None:
        self._text = text
        self.converse_bundles: list[str] = []
        self.decide_calls = 0

    async def decide_action(self, bundle, schema, *, tier, actor_id, tick):
        self.decide_calls += 1
        return {
            "action_kind": "share",
            "intent": "오늘 있었던 일을 짧은 근황으로 남긴다",
            "target_actor_id": None,
            "location_id": None,
            "params": {},
            "decision_trace": {"trace_id": f"stub-{actor_id}-{tick}", "tier": tier},
        }

    async def converse(self, bundle, *, tier, actor_id, tick):
        self.converse_bundles.append(bundle.user)
        return self._text


def make_phases(redis, ai, *, mailbox=None, outreach=None) -> ActorPhases:
    return ActorPhases(
        [make_host()],
        ai=ai,
        memory=WorkingMemory(redis),
        mailbox=mailbox,
        relationship=RelationshipAdapter(redis),
        outreach=outreach,
    )


def messages_of(events: list[dict]) -> list[dict]:
    return [e for e in events if e["type"] == "actor.message.sent"]


# ---- 순수 단위 — 후보 선정·규칙 인사 템플릿 ----


def test_pick_dm_target_filters_players_and_threshold():
    """플레이어 엣지만 후보다 — 임계(trust+intimacy) 미만·액터 엣지는 제외."""
    edges = {
        "a_probe_friend": edge_state(0.9, 0.9),   # 액터 — 후보 아님
        "p_probe_cold": edge_state(0.1, 0.1),     # 임계 미만
        "p_probe_fan": edge_state(0.4, 0.3),      # 0.7 ≥ 0.5
    }
    assert pick_dm_target(edges, threshold=0.5) == "p_probe_fan"
    assert pick_dm_target({"p_probe_cold": edge_state(0.1, 0.1)}, threshold=0.5) is None
    assert pick_dm_target({}, threshold=0.5) is None


def test_pick_dm_target_picks_highest_and_tie_breaks_deterministically():
    """임계를 넘은 여럿 중 최고 관계 1명 — 동률은 id 순 (결정적)."""
    edges = {
        "p_probe_b": edge_state(0.4, 0.2),
        "p_probe_a": edge_state(0.5, 0.4),  # 최고
    }
    assert pick_dm_target(edges, threshold=0.5) == "p_probe_a"
    tied = {"p_probe_b": edge_state(0.3, 0.3), "p_probe_a": edge_state(0.3, 0.3)}
    assert pick_dm_target(tied, threshold=0.5) == "p_probe_a"


def test_fallback_greeting_is_deterministic_rotating_and_clean():
    """규칙 인사 풀 — 3종 이상 회전, 같은 입력이면 같은 문장, 기계 표기 비노출."""
    persona = make_host()
    assert fallback_greeting(persona, PLAYER) == fallback_greeting(persona, PLAYER)
    texts = {fallback_greeting(persona, f"p_probe_{i}") for i in range(64)}
    assert len(texts) >= 3  # 풀이 실제로 회전한다
    for text in texts:
        assert persona.name not in text  # 페르소나 이름 비노출
        assert "a_" not in text and "p_" not in text  # 기계 표기 비노출


def test_outreach_memo_joins_conversation_history():
    """선제 DM 메모는 대화 히스토리의 내 발화로 재구성된다 — 다음 답장이 흐름을 잇는다."""
    memo = f'tick 7: 나는 플레이어 {PLAYER}에게 먼저 안부를 건넸다 — "요즘 어때요?"'
    assert conversation_turns([memo], PLAYER) == [("me", "요즘 어때요?")]
    assert conversation_turns([memo], "p_probe_other") == []


async def test_outreach_ledger_survives_restart(redis):
    """빈도 장부는 Redis에 산다 — 재시작해도 하루 1회 가드가 이어진다."""
    ledger = OutreachLedger(redis)
    assert await ledger.last_sent_tick(WORLD, ACTOR, PLAYER) is None
    await ledger.mark(WORLD, ACTOR, PLAYER, 42)
    assert await OutreachLedger(redis).last_sent_tick(WORLD, ACTOR, PLAYER) == 42


# ---- ① 첫 접촉 인사 (통합) ----


async def test_first_like_gets_greeting_comment_via_llm(conn, redis):
    """엣지 없는 플레이어의 첫 좋아요 → 그 포스트에 가벼운 인사 댓글 (plan/03)."""
    mailbox = Mailbox(redis)
    like = like_envelope()
    await mailbox.push(WORLD, ACTOR, like)
    ai = _ConverseAi("어, 처음 뵙는 분이네요. 좋아요 고마워요.")
    phases = make_phases(redis, ai, mailbox=mailbox)
    phases._lods[ACTOR] = ActorLod(tier=Tier.COLD, last_interest_tick=0)

    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    [greeting] = messages_of(events)
    assert greeting["payload"]["channel"] == "comment"
    assert greeting["payload"]["target_player_id"] == PLAYER
    assert greeting["payload"]["post_id"] == like["payload"]["post_id"]
    assert greeting["payload"]["in_reply_to"] == like["event_id"]
    assert greeting["causation_id"] == like["event_id"]
    assert greeting["correlation_id"] == like["correlation_id"]
    assert greeting["payload"]["text"] == "어, 처음 뵙는 분이네요. 좋아요 고마워요."

    # 인사 결의 Task Frame이 컨텍스트에 놓였다 (가벼운 첫 인사 — 과하지 않게)
    assert any("첫 인사" in b for b in ai.converse_bundles)
    # 좋아요는 여전히 관심 신호일 뿐 — 잠든 액터를 깨우지 않는다 (기존 LOD 규칙)
    assert phases._lods[ACTOR].tier is Tier.COLD
    # 자기 인사가 작업 기억에 남았다 — 이후 대화가 흐름을 잇는다
    recent = await WorkingMemory(redis).recent(WORLD, ACTOR)
    assert any(f"플레이어 {PLAYER}에게 답했다" in m for m in recent)


async def test_first_like_greeting_falls_back_to_rule_template(conn, redis):
    """AI 실패에도 첫 인사는 보증된다 — 결정적 규칙 템플릿 풀 (첫 개입 무반응 금지)."""
    mailbox = Mailbox(redis)
    await mailbox.push(WORLD, ACTOR, like_envelope())
    phases = make_phases(redis, _SilentAi(), mailbox=mailbox)

    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    [greeting] = messages_of(events)
    assert greeting["payload"]["text"] == fallback_greeting(make_host(), PLAYER)


async def test_second_like_next_tick_stays_silent(conn, redis):
    """첫 좋아요가 응고시킨 엣지 위의 다음 좋아요 — 기존 그대로 무반응이다."""
    mailbox = Mailbox(redis)
    await mailbox.push(WORLD, ACTOR, like_envelope(tick=0))
    phases = make_phases(redis, _SilentAi(), mailbox=mailbox)
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    await mailbox.push(WORLD, ACTOR, like_envelope(tick=1))
    await run_tick(conn, phases, CLOCK, WORLD, tick=1, head=head)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    assert len(messages_of(events)) == 1  # tick 0의 첫 인사뿐 — 이후는 침묵
    # 두 번째 좋아요도 지각은 된다 (감정·관계 입력, ADR-015/016)
    recent = await WorkingMemory(redis).recent(WORLD, ACTOR)
    assert sum("좋아요" in m for m in recent) >= 2


async def test_like_on_existing_edge_stays_silent(conn, redis):
    """이미 아는 사이(엣지 있음)의 좋아요는 첫 접촉이 아니다 — 인사 없음."""
    mailbox = Mailbox(redis)
    await seed_edge(redis, ACTOR, PLAYER, edge_state(0.2, 0.1))
    await mailbox.push(WORLD, ACTOR, like_envelope())
    phases = make_phases(redis, _SilentAi(), mailbox=mailbox)

    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    assert messages_of(events) == []


async def test_greetings_capped_one_per_actor_per_tick(conn, redis):
    """같은 tick의 첫 접촉 여럿 — 인사는 액터당 1건 상한 (params outreach 노브)."""
    mailbox = Mailbox(redis)
    await mailbox.push(WORLD, ACTOR, like_envelope("p_probe_first"))
    await mailbox.push(WORLD, ACTOR, like_envelope("p_probe_second"))
    phases = make_phases(redis, _SilentAi(), mailbox=mailbox)

    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    [greeting] = messages_of(events)
    assert greeting["payload"]["target_player_id"] == "p_probe_first"  # 도착 순 첫 1건


# ---- ② '기억됨' 선제 DM (통합) ----


def rhythm_only_tick(actor_id: str, lifestyle: str, *, skip: int = 0) -> int:
    """포스팅 모먼트이면서 LOD(Cold) due는 아닌 tick — 리듬 경로만 발화한다."""
    params = default_params()
    lod = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    found = 0
    for tick in range(TICKS_PER_DAY):
        if posting_moment(
            actor_id, lifestyle, CLOCK.world_time_at(tick), params
        ) and not is_due(actor_id, lod, tick):
            if found == skip:
                return tick
            found += 1
    raise AssertionError("포스팅 모먼트 tick을 찾지 못했다 — 파라미터를 확인하라")


async def test_rhythm_moment_sends_proactive_dm_and_replaces_posting(conn, redis):
    """임계를 넘은 관계가 있으면 리듬 모먼트가 선제 DM이 된다 — 포스팅 대체 (plan/02)."""
    persona = make_host()
    await seed_edge(redis, ACTOR, PLAYER, edge_state(0.4, 0.3))  # 0.7 ≥ 0.5
    await WorkingMemory(redis).add(WORLD, ACTOR, f'플레이어 {PLAYER}의 DM: "요즘 잘 지내요?"')
    ai = _ConverseAi("문득 생각나서요. 그때 그 얘기 덕분에 잘 지내요.")
    ledger = OutreachLedger(redis)
    phases = make_phases(redis, ai, outreach=ledger)
    phases._lods[ACTOR] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    tick = rhythm_only_tick(ACTOR, persona.lifestyle)

    await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    [dm] = messages_of(events)
    assert dm["payload"]["channel"] == "dm"
    assert dm["payload"]["target_player_id"] == PLAYER
    assert dm["payload"]["post_id"] is None
    assert dm["payload"]["in_reply_to"] is None  # 원인 플레이어 이벤트가 없다
    assert dm["causation_id"] is None
    assert dm["payload"]["text"] == "문득 생각나서요. 그때 그 얘기 덕분에 잘 지내요."

    # DM이 포스팅을 대체했다 — 둘 다 하지 않는다 (자연스러움)
    assert [e for e in events if e["type"] == "actor.action.performed"] == []
    assert ai.decide_calls == 0
    # 함께 나눈 기억이 컨텍스트에 곁들여졌다 — 대화 히스토리 + 안부의 결 Task Frame
    [bundle] = ai.converse_bundles
    assert "요즘 잘 지내요?" in bundle and "안부" in bundle
    # 빈도 장부에 남았다 — 세계 하루 1회 가드의 근거
    assert await ledger.last_sent_tick(WORLD, ACTOR, PLAYER) == tick
    # 자기 발화가 작업 기억에 남았다 — 이후 대화가 흐름을 잇는다
    recent = await WorkingMemory(redis).recent(WORLD, ACTOR)
    assert any("먼저 안부를 건넸다" in m for m in recent)


async def test_proactive_dm_cooldown_one_per_world_day(conn, redis):
    """같은 (액터,플레이어)에게 세계 하루(360 tick) 안에 두 번 보내지 않는다."""
    persona = make_host()
    await seed_edge(redis, ACTOR, PLAYER, edge_state(0.4, 0.3))
    ai = _ConverseAi("오늘도 문득 생각나서요.")
    ledger = OutreachLedger(redis)
    phases = make_phases(redis, ai, outreach=ledger)
    phases._lods[ACTOR] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    first = rhythm_only_tick(ACTOR, persona.lifestyle)
    second = rhythm_only_tick(ACTOR, persona.lifestyle, skip=1)
    assert second - first < 360  # 같은 세계 하루 안이다

    head = await run_tick(conn, phases, CLOCK, WORLD, tick=first, head=0)
    await run_tick(conn, phases, CLOCK, WORLD, tick=second, head=head)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    assert len(messages_of(events)) == 1  # 첫 모먼트의 DM뿐
    # 두 번째 모먼트는 원래의 리듬 포스팅 경로로 이어졌다
    assert ai.decide_calls == 1
    assert [e for e in events if e["type"] == "actor.action.performed"]


async def test_proactive_dm_below_threshold_keeps_posting(conn, redis):
    """임계 미만 관계뿐이면 선제 DM은 없다 — 리듬 포스팅은 기존 그대로."""
    persona = make_host()
    await seed_edge(redis, ACTOR, PLAYER, edge_state(0.2, 0.1))  # 0.3 < 0.5
    ai = _ConverseAi("보내지 않아야 할 문장")
    phases = make_phases(redis, ai, outreach=OutreachLedger(redis))
    phases._lods[ACTOR] = ActorLod(tier=Tier.COLD, last_interest_tick=0)

    await run_tick(conn, phases, CLOCK, WORLD, tick=rhythm_only_tick(ACTOR, persona.lifestyle),
                   head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    assert messages_of(events) == []
    assert ai.converse_bundles == []  # DM 시도 자체가 없다
    assert [e for e in events if e["type"] == "actor.action.performed"]  # 포스팅은 그대로


async def test_proactive_dm_ai_failure_skips_silently_without_burning_cooldown(conn, redis):
    """AI 실패면 조용히 생략 — 규칙 문장 금지, 쿨다운 미소모 (dev 결정성 불변)."""
    persona = make_host()
    await seed_edge(redis, ACTOR, PLAYER, edge_state(0.4, 0.3))
    ledger = OutreachLedger(redis)
    phases = make_phases(redis, _SilentAi(), outreach=ledger)
    phases._lods[ACTOR] = ActorLod(tier=Tier.COLD, last_interest_tick=0)

    await run_tick(conn, phases, CLOCK, WORLD, tick=rhythm_only_tick(ACTOR, persona.lifestyle),
                   head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    assert messages_of(events) == []  # 선제 안부를 규칙 문장으로 보내지 않는다
    assert await ledger.last_sent_tick(WORLD, ACTOR, PLAYER) is None  # 쿨다운 미소모


async def test_no_outreach_ledger_means_no_proactive_dm(conn, redis):
    """장부 미주입(기존 배선)이면 선제 DM 경로 자체가 없다 — 후방 호환."""
    persona = make_host()
    await seed_edge(redis, ACTOR, PLAYER, edge_state(0.9, 0.9))
    ai = _ConverseAi("보내지 않아야 할 문장")
    phases = make_phases(redis, ai)  # outreach=None
    phases._lods[ACTOR] = ActorLod(tier=Tier.COLD, last_interest_tick=0)

    await run_tick(conn, phases, CLOCK, WORLD, tick=rhythm_only_tick(ACTOR, persona.lifestyle),
                   head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    assert messages_of(events) == []
    assert ai.converse_bundles == []
