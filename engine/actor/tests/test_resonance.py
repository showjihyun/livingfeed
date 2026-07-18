"""드라마 재생산 여운 (plan/02 §측정) — 강렬했던 플레이어 댓글 교환이 후속 포스트로.

사슬: 플레이어 댓글(correlation=원 포스트) → 액터 응답 + 감정 shift(임계 이상)
→ 여운 기록(Redis, 소모품) → 리듬 모먼트에 원 correlation을 승계한
actor.action.performed 분출 → composer가 feed.post.published로 승격(승계는
build_post_event가 이미 한다 — engine/feed test_compose가 고정). 승계가 곧
'이 대화가 낳은 이야기' 배지와 loop_health 드라마 재생산율 분자의 판정이다.

과열 가드: correlation당 1회 소모·액터당 여운 1개·분출 쿨다운·TTL(바램).
프로브 페르소나 즉석 조립 — 특정 인물 하드코딩 없음 (사용자 표준).
PG+Redis 필요 (conftest 게이트).
"""

from datetime import UTC, datetime

from lf_actor.emotion import PendingShift
from lf_actor.mailbox import Mailbox
from lf_actor.memory import WorkingMemory
from lf_actor.persona import Persona
from lf_actor.phases import ActorPhases
from lf_actor.resonance import Resonance, ResonanceStore
from lf_actor.rhythm import default_params, posting_moment
from lf_actor.rules import fallback_follow_up, fallback_reply
from lf_eventstore import new_ulid, read_stream
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick
from lf_tick.lod import ActorLod, Tier, is_due

WORLD = "w_resonance"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))
ACTOR = "a_probe_muse"
PLAYER = "p_probe_visitor"
LIFESTYLE = "flexible"
TICKS_PER_DAY = 360

COMMENT_TEXT = "그때 그 글, 저한테는 정말 큰 위로였어요"


def make_muse() -> Persona:
    return Persona(
        id=ACTOR,
        name="사색가",
        archetype="probe_muse",
        identity_core="한 번의 대화를 오래 굴리는 주민.",
        big_five={"openness": 0.8, "conscientiousness": 0.5, "extraversion": 0.4,
                  "agreeableness": 0.7, "neuroticism": 0.5},
        needs_bias={"belonging": 0.7, "achievement": 0.5, "security": 0.4},
        goals=({"id": "g_words", "description": "마음에 남는 문장 쓰기",
                "priority": 0.8, "need": "achievement"},),
        lifestyle=LIFESTYLE,
    )


def comment_envelope(*, tick: int = 0, text: str = COMMENT_TEXT) -> dict:
    """플레이어 댓글 봉투 — correlation은 원 포스트 id (gateway 규약)."""
    event_id = new_ulid()
    post_id = new_ulid()
    return {
        "event_id": event_id, "stream": "player", "type": "player.comment.posted",
        "schema_version": 1, "world_id": WORLD, "actor_id": None, "tick": tick,
        "occurred_at": "2026-03-01T00:00:00Z", "causation_id": None,
        "correlation_id": post_id,
        "payload": {"player_id": PLAYER, "target_actor_id": ACTOR,
                    "post_id": post_id, "text": text},
    }


def make_resonance(
    *, correlation_id: str | None = None, intensity: float = 0.8, tick: int = 0
) -> Resonance:
    return Resonance(
        correlation_id=correlation_id or new_ulid(),
        source_event_id=new_ulid(),
        player_id=PLAYER,
        comment=COMMENT_TEXT,
        reply="고마워요. 그 말이 오늘 하루를 바꿨어요.",
        intensity=intensity,
        tick=tick,
    )


class _SilentAi:
    """LLM 없는 세계 — 규칙 폴백만 돈다 (dev 결정성)."""

    async def decide_action(self, bundle, schema, *, tier, actor_id, tick):
        return None

    async def converse(self, bundle, *, tier, actor_id, tick):
        return None

    async def reflect(self, *args, **kwargs):
        return None


class _DecideAi(_SilentAi):
    """decide_action에 응답하는 스텁 — 컨텍스트(bundle)를 기록한다."""

    def __init__(self) -> None:
        self.decide_bundles: list[str] = []

    async def decide_action(self, bundle, schema, *, tier, actor_id, tick):
        self.decide_bundles.append(bundle.user)
        return {
            "action_kind": "speak",
            "intent": "그 대화 이후로 생각이 조금 바뀌었다 — 오늘은 그 이야기를 쓴다",
            "headline": "마음에 남은 대화에 대하여",
            "target_actor_id": None,
            "location_id": None,
            "params": {},
            "decision_trace": {"trace_id": f"stub-{actor_id}-{tick}", "tier": tier},
        }


class _EmotionStub:
    """감정 평가 스텁 — 플레이어 댓글마다 지정 강도의 shift 하나 (여운 임계 제어).

    payload는 actor.emotion.shifted 스키마를 지킨다 — resolve가 실제로 적재한다.
    """

    def __init__(self, intensity: float) -> None:
        self._intensity = intensity

    async def appraise(self, world_id, persona, interactions, *, tick, edges=None):
        shifts = []
        for envelope in interactions:
            if envelope["type"] != "player.comment.posted":
                continue
            player = envelope["payload"]["player_id"]
            instance = {"type": "joy", "intensity": self._intensity, "target_id": player}
            shifts.append(PendingShift(
                actor_id=persona.id,
                payload={
                    "mood": {"pleasure": 0.4, "arousal": 0.3, "dominance": 0.0},
                    "emotions": [instance],
                    "reason": "마음을 두드린 댓글을 받았다",
                },
                causation_id=envelope["event_id"],
                correlation_id=envelope["correlation_id"],
                instance=instance,
            ))
        return shifts, "기분: 잔잔히 들뜸"

    async def decay_ticks(self, world_id, persona, ticks=1):
        return None


def make_phases(redis, ai, *, mailbox=None, emotion=None, resonance=None) -> ActorPhases:
    return ActorPhases(
        [make_muse()],
        ai=ai,
        memory=WorkingMemory(redis),
        mailbox=mailbox,
        emotion=emotion,
        resonance=resonance,
    )


def actions_of(events: list[dict], *, tick: int | None = None) -> list[dict]:
    return [
        e for e in events
        if e["type"] == "actor.action.performed" and (tick is None or e["tick"] == tick)
    ]


def rhythm_only_tick(*, after: int = 1) -> int:
    """after 이후 첫 포스팅 모먼트이면서 LOD(Cold) due는 아닌 tick — 분출 단독 검증용."""
    params = default_params()
    lod = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    for tick in range(after + 1, TICKS_PER_DAY):
        if posting_moment(
            ACTOR, LIFESTYLE, CLOCK.world_time_at(tick), params
        ) and not is_due(ACTOR, lod, tick):
            return tick
    raise AssertionError("포스팅 모먼트 tick을 찾지 못했다 — 파라미터를 확인하라")


def any_moment_tick() -> int:
    """due 여부와 무관한 첫 포스팅 모먼트 tick — Hot 공존 검증용."""
    params = default_params()
    for tick in range(1, TICKS_PER_DAY):
        if posting_moment(ACTOR, LIFESTYLE, CLOCK.world_time_at(tick), params):
            return tick
    raise AssertionError("포스팅 모먼트 tick을 찾지 못했다 — 파라미터를 확인하라")


# ---- 순수 단위 — 규칙 폴백 문장 ----


def test_fallback_follow_up_is_deterministic_and_clean():
    """같은 (페르소나, tick)이면 같은 문장 — 원시 id·기계 어휘 비노출 (리시트 정화의 결)."""
    persona = make_muse()
    a = fallback_follow_up(persona, 7, "t-1", fragment=COMMENT_TEXT)
    b = fallback_follow_up(persona, 7, "t-1", fragment=COMMENT_TEXT)
    assert a == b
    assert a["action_kind"] == "speak" and a["target_actor_id"] is None
    assert a["params"] == {"fallback": True, "resonance": True}
    assert a["decision_trace"]["tier"] == "cold_rule"
    assert COMMENT_TEXT in a["intent"]  # 여운 요지가 인용으로 스며든다
    for text in (a["intent"], a["headline"]):
        assert "p_" not in text and "a_" not in text  # 플레이어·액터 원시 id 금지
        assert "correlation" not in text and "resonance" not in text  # 기계 어휘 금지


def test_fallback_follow_up_rotates_across_ticks():
    """tick 회전으로 문장·헤드라인 풀이 실제로 돈다 — 도배 결 방지."""
    persona = make_muse()
    intents = {
        fallback_follow_up(persona, t, "t", fragment="짧은 말")["intent"] for t in range(30)
    }
    assert len(intents) >= 3


# ---- 저장소 단위 (Redis) ----


async def test_store_keeps_strongest_resonance_per_actor(redis):
    """액터당 여운 1개 — 더 강한 것만 자리를 대체하고, 약한 것은 거부된다."""
    store = ResonanceStore(redis)
    strong = make_resonance(intensity=0.8, tick=1)
    assert await store.record(WORLD, ACTOR, strong) is True
    weaker = make_resonance(intensity=0.7, tick=2)
    assert await store.record(WORLD, ACTOR, weaker) is False
    loaded = await store.load(WORLD, ACTOR)
    assert loaded == strong
    stronger = make_resonance(intensity=0.9, tick=3)
    assert await store.record(WORLD, ACTOR, stronger) is True
    assert await ResonanceStore(redis).load(WORLD, ACTOR) == stronger  # 재시작에도 남는다


async def test_store_refuses_spent_correlation(redis):
    """분출한 사슬은 끝났다 — 같은 correlation의 재기록은 거부된다 (재분출 없음)."""
    store = ResonanceStore(redis)
    first = make_resonance(intensity=0.8, tick=1)
    await store.record(WORLD, ACTOR, first)
    await store.spend(WORLD, ACTOR, first.correlation_id, 10)
    assert await store.load(WORLD, ACTOR) is None  # 분출로 소모됐다
    assert await store.last_burst_tick(WORLD, ACTOR) == 10  # 쿨다운 장부
    again = make_resonance(
        correlation_id=first.correlation_id, intensity=0.95, tick=11
    )
    assert await store.record(WORLD, ACTOR, again) is False
    other = make_resonance(intensity=0.7, tick=12)  # 다른 대화의 여운은 새로 남는다
    assert await store.record(WORLD, ACTOR, other) is True


# ---- 여운 기록 (통합) ----


async def test_intense_comment_exchange_leaves_resonance(conn, redis):
    """임계 이상 감정의 댓글 교환 → correlation·요지·tick이 여운으로 남는다."""
    mailbox = Mailbox(redis)
    comment = comment_envelope()
    await mailbox.push(WORLD, ACTOR, comment)
    store = ResonanceStore(redis)
    phases = make_phases(
        redis, _DecideAi(), mailbox=mailbox, emotion=_EmotionStub(0.8), resonance=store
    )

    await run_tick(conn, phases, CLOCK, WORLD, tick=3, head=0)

    res = await store.load(WORLD, ACTOR)
    assert res is not None
    assert res.correlation_id == comment["correlation_id"]  # 원 포스트의 사슬
    assert res.source_event_id == comment["event_id"]
    assert res.player_id == PLAYER
    assert res.comment == COMMENT_TEXT
    assert res.reply == fallback_reply(make_muse(), COMMENT_TEXT)  # 실제 답장 요지
    assert res.intensity == 0.8 and res.tick == 3


async def test_mild_exchange_leaves_no_resonance(conn, redis):
    """유의미(shift 발행)하지만 임계 미만의 감정 — 여운은 남지 않는다 (보수적 임계)."""
    mailbox = Mailbox(redis)
    await mailbox.push(WORLD, ACTOR, comment_envelope())
    store = ResonanceStore(redis)
    phases = make_phases(
        redis, _DecideAi(), mailbox=mailbox, emotion=_EmotionStub(0.4), resonance=store
    )

    await run_tick(conn, phases, CLOCK, WORLD, tick=3, head=0)

    assert await store.load(WORLD, ACTOR) is None


async def test_dm_exchange_leaves_no_resonance(conn, redis):
    """DM은 사적 대화 — 여운(공개 후속 포스트)의 재료가 아니다 (댓글 교환만)."""
    mailbox = Mailbox(redis)
    event_id = new_ulid()
    await mailbox.push(WORLD, ACTOR, {
        "event_id": event_id, "stream": "player", "type": "player.dm.sent",
        "schema_version": 1, "world_id": WORLD, "actor_id": None, "tick": 3,
        "occurred_at": "2026-03-01T00:00:00Z", "causation_id": None,
        "correlation_id": event_id,
        "payload": {"player_id": PLAYER, "target_actor_id": ACTOR, "text": COMMENT_TEXT},
    })
    store = ResonanceStore(redis)
    phases = make_phases(
        redis, _DecideAi(), mailbox=mailbox, emotion=_EmotionStub(0.9), resonance=store
    )

    await run_tick(conn, phases, CLOCK, WORLD, tick=3, head=0)

    assert await store.load(WORLD, ACTOR) is None


# ---- 분출 (통합) — correlation 승계가 판정 그 자체다 ----


async def test_resonance_bursts_follow_up_post_inheriting_correlation(conn, redis):
    """기록→분출 전 과정: 리듬 모먼트의 후속 포스트가 원 대화의 사슬을 승계한다."""
    mailbox = Mailbox(redis)
    comment = comment_envelope()
    await mailbox.push(WORLD, ACTOR, comment)
    store = ResonanceStore(redis)
    ai = _DecideAi()
    phases = make_phases(
        redis, ai, mailbox=mailbox, emotion=_EmotionStub(0.8), resonance=store
    )
    moment = rhythm_only_tick()
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=moment - 1, head=0)
    assert await store.load(WORLD, ACTOR) is not None  # 여운이 남았다

    # 댓글이 올린 Hot을 되돌린다 — 분출 단독 경로(비-due 모먼트)를 격리 검증
    phases._lods[ACTOR] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    await run_tick(conn, phases, CLOCK, WORLD, tick=moment, head=head)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    [burst] = actions_of(events, tick=moment)
    # 핵심 판정 — es 봉투가 원 대화의 correlation을 승계했다 (배지·loop_health 분자)
    assert burst["correlation_id"] == comment["correlation_id"]
    assert burst["causation_id"] == comment["event_id"]
    assert burst["payload"]["action_kind"] == "speak"
    # 여운 요지가 decide 재료로 놓였고 상대는 세계 안 어휘다 ('그 사람')
    assert any("마음에 남은 대화" in b and COMMENT_TEXT in b for b in ai.decide_bundles)
    assert any("그 사람이 남긴 말" in b for b in ai.decide_bundles)
    # 소모·쿨다운 — 같은 여운은 두 번 터지지 않는다
    assert await store.load(WORLD, ACTOR) is None
    assert await store.last_burst_tick(WORLD, ACTOR) == moment


async def test_burst_fallback_sentence_is_clean_and_still_inherits(conn, redis):
    """AI 실패에도 분출은 보증 — 규칙 문장으로, 사슬 승계는 그대로 (조용한 생략 금지)."""
    store = ResonanceStore(redis)
    moment = rhythm_only_tick()
    res = make_resonance(tick=moment - 1)
    await store.record(WORLD, ACTOR, res)
    phases = make_phases(redis, _SilentAi(), resonance=store)
    phases._lods[ACTOR] = ActorLod(tier=Tier.COLD, last_interest_tick=0)

    await run_tick(conn, phases, CLOCK, WORLD, tick=moment, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    [burst] = actions_of(events, tick=moment)
    assert burst["correlation_id"] == res.correlation_id
    payload = burst["payload"]
    assert payload["params"] == {"fallback": True, "resonance": True}
    assert COMMENT_TEXT in payload["intent"]  # 그 대화의 조각이 인용된다
    assert PLAYER not in payload["intent"] and "p_" not in payload["intent"]


async def test_faded_resonance_never_bursts(conn, redis):
    """TTL을 넘긴 여운은 바랬다 — 분출하지 않고 지워진다 (여운은 바래는 것)."""
    params = default_params()["resonance"]
    ttl = int(params["ttl_ticks"])
    moment = rhythm_only_tick(after=ttl + 1)
    store = ResonanceStore(redis)
    await store.record(WORLD, ACTOR, make_resonance(tick=moment - ttl - 1))
    phases = make_phases(redis, _SilentAi(), resonance=store)
    phases._lods[ACTOR] = ActorLod(tier=Tier.COLD, last_interest_tick=0)

    await run_tick(conn, phases, CLOCK, WORLD, tick=moment, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    assert actions_of(events, tick=moment) == []  # 분출 없음 (리듬도 AI 실패로 생략)
    assert await store.load(WORLD, ACTOR) is None  # 바랜 여운은 지워졌다
    assert await store.last_burst_tick(WORLD, ACTOR) is None  # 쿨다운 미소모


async def test_burst_cooldown_blocks_next_resonance(conn, redis):
    """분출 쿨다운 안의 다음 여운은 기다린다 — 소모되지 않고 남는다 (과열 가드)."""
    cooldown = int(default_params()["resonance"]["cooldown_ticks"])
    moment = rhythm_only_tick()
    store = ResonanceStore(redis)
    await store.spend(WORLD, ACTOR, new_ulid(), max(0, moment - 5))  # 직전 분출의 흔적
    assert moment - max(0, moment - 5) < cooldown  # 아직 쿨다운 안이다
    res = make_resonance(tick=moment - 1)
    await store.record(WORLD, ACTOR, res)
    phases = make_phases(redis, _SilentAi(), resonance=store)
    phases._lods[ACTOR] = ActorLod(tier=Tier.COLD, last_interest_tick=0)

    await run_tick(conn, phases, CLOCK, WORLD, tick=moment, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    assert actions_of(events, tick=moment) == []
    assert await store.load(WORLD, ACTOR) == res  # 여운은 남아 다음 모먼트를 기다린다


async def test_hot_actor_bursts_alongside_due_action(conn, redis):
    """Hot 액터도 모먼트에 분출한다 — LOD 결합의 역설 차단 (선제 DM의 교훈).

    행동과 분출이 한 tick에 공존한다: 여운은 행동이 아니라 마음이다.
    """
    store = ResonanceStore(redis)
    moment = any_moment_tick()
    res = make_resonance(tick=moment - 1)
    await store.record(WORLD, ACTOR, res)
    phases = make_phases(redis, _DecideAi(), resonance=store)
    phases._lods[ACTOR] = ActorLod(tier=Tier.HOT, last_interest_tick=moment)

    await run_tick(conn, phases, CLOCK, WORLD, tick=moment, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", ACTOR)]
    actions = actions_of(events, tick=moment)
    assert len(actions) == 2  # 일과 행동 + 여운 분출
    inherited = [a for a in actions if a["correlation_id"] == res.correlation_id]
    assert len(inherited) == 1  # 사슬 승계는 분출 하나뿐 — 일과는 새 사슬의 루트다
