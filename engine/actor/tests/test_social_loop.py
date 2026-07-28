"""액터 소셜 루프 통합 — 지각→감정, 자발 댓글, 작성자 답글 의무 (ADR-012/015 확장).

메일박스에 배달된 feed.post.published / 액터 댓글이 인지 루프를 타는지 검증한다.
라우터 배달 자체는 test_social.py(대상 선정)의 몫이다 — 여기서는 배달된 뒤의 삶이다.
PG+Redis 필요 (conftest 게이트).
"""

from datetime import UTC, datetime

import pytest
from lf_actor.client import Inference
from lf_actor.emotion import EmotionAdapter
from lf_actor.mailbox import Mailbox
from lf_actor.memory import WorkingMemory
from lf_actor.persona import Persona
from lf_actor.phases import ActorPhases
from lf_actor.relationship import RelationshipAdapter
from lf_actor.social import comment_targets
from lf_eventstore import new_ulid, read_stream
from lf_relationship import RelationshipState
from lf_relationship import default_params as relationship_params
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick
from lf_tick.lod import ActorLod, Tier

WORLD = "w_social"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))
WRITER = "a_probe_writer"
READER = "a_probe_reader"


def make_writer() -> Persona:
    return Persona(
        id=WRITER, name="글쓴이", archetype="probe_writer",
        identity_core="쓰는 일로 하루를 시작하는 사람.",
        big_five={"openness": 0.7, "conscientiousness": 0.6, "extraversion": 0.5,
                  "agreeableness": 0.6, "neuroticism": 0.4},
        needs_bias={"achievement": 0.8, "belonging": 0.5, "security": 0.4},
        goals=({"id": "g_write", "description": "긴 글 하나를 완성하기",
                "priority": 0.8, "need": "achievement"},),
    )


def make_reader() -> Persona:
    return Persona(
        id=READER, name="읽는이", archetype="probe_reader",
        identity_core="남의 근황을 오래 들여다보는 사람.",
        big_five={"openness": 0.6, "conscientiousness": 0.5, "extraversion": 0.7,
                  "agreeableness": 0.7, "neuroticism": 0.4},
        needs_bias={"belonging": 0.9, "achievement": 0.4, "security": 0.5},
        goals=({"id": "g_people", "description": "마음 붙일 사람들 찾기",
                "priority": 0.8, "need": "belonging"},),
    )


def feed_post(author: str = WRITER, *, title: str = "글쓴이, 이야기를 꺼내다") -> dict:
    post_id = new_ulid()
    return {
        "event_id": post_id, "stream": "feed", "type": "feed.post.published",
        "schema_version": 1, "world_id": WORLD, "actor_id": author, "tick": 3,
        "occurred_at": "2026-03-01T00:12:00Z", "causation_id": new_ulid(),
        "correlation_id": post_id,
        "payload": {
            "visibility": "world", "title": title,
            "body": "오래 미뤄둔 글을 오늘부터 다시 쓴다. 설레는 시작이다.",
            "narration_kind": "template", "participants": [author],
            "community_id": None, "location_id": None,
            "drama_score": 0.6, "worthiness": 0.5,
            "source_event_type": "actor.action.performed",
            "tags": ["work"], "media": [],
        },
    }


async def warm_edge(redis, reader: str, author: str) -> None:
    """독자→작성자 온기 있는 관계 — 글이 마음을 울릴 조건."""
    adapter = RelationshipAdapter(redis)
    state = RelationshipState(
        dimensions={"trust": 0.7, "intimacy": 0.8, "respect": 0.3,
                    "attraction": 0.0, "resentment": 0.0},
        stage="friend", salience=0.8,
    )
    await adapter.save(WORLD, reader, author, state)
    await redis.sadd(f"lf:relidx:{WORLD}:{reader}", author)


class _SilentAi:
    """LLM 없는 세계 — 규칙 폴백만 돈다 (dev 결정성)."""

    async def decide_action(self, bundle, schema, *, tier, actor_id, tick):
        return Inference(None)

    async def converse(self, bundle, *, tier, actor_id, tick):
        return Inference(None)

    async def reflect(self, *args, **kwargs):
        return Inference(None)


async def test_feed_post_is_perceived_without_waking_sleeper(conn, redis):
    """② 지각+감정: 피드 글이 기억과 감정에 남지만 잠든 액터를 깨우진 않는다."""
    reader, writer = make_reader(), make_writer()
    mailbox = Mailbox(redis)
    await warm_edge(redis, READER, WRITER)
    post = feed_post()
    await mailbox.push(WORLD, READER, post)

    phases = ActorPhases(
        [reader, writer],
        ai=_SilentAi(),
        memory=WorkingMemory(redis),
        mailbox=mailbox,
        emotion=EmotionAdapter(redis),
        relationship=RelationshipAdapter(redis),
    )
    # 남의 글은 잠든 사람을 깨우지도, 관심 시계를 되돌리지도 않는다 (ADR-011).
    # 배달=touch였을 땐 활발한 세계에서 전원이 상시 Hot을 유지했다 (과열 관측)
    phases._lods[READER] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    await run_tick(conn, phases, CLOCK, WORLD, tick=5, head=0)

    assert phases._lods[READER].tier is Tier.COLD  # 깨어나지 않았다
    assert phases._lods[READER].last_interest_tick == 0  # 관심 시계도 그대로다

    # 지각이 기억에 남았다 — 작성자 이름과 제목·본문 일부
    recent = await WorkingMemory(redis).recent(WORLD, READER)
    assert any("피드에서 글쓴이의 글을 봤다" in m and "이야기를 꺼내다" in m for m in recent)

    # 온기 있는 관계의 글 → 잔잔한 기쁨이 이벤트로 남았다 (actor.emotion.shifted)
    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", READER)]
    shifts = [e for e in events if e["type"] == "actor.emotion.shifted"]
    assert shifts, "피드 글의 감흥이 감정 이벤트로 남았어야 한다"
    assert shifts[0]["causation_id"] == post["event_id"]
    assert any(e["target_id"] == WRITER for e in shifts[0]["payload"]["emotions"])


class _CommentingAi:
    """마음이 움직인 LLM을 재현하는 스텁 — 본 글이 스키마에 열렸을 때만 댓글을 단다."""

    def __init__(self) -> None:
        self.bundles: list[str] = []
        self.schemas: list[dict] = []

    async def decide_action(self, bundle, schema, *, tier, actor_id, tick):
        self.bundles.append(bundle.user)
        self.schemas.append(schema)
        comment_path = schema.get("properties", {}).get("comment")
        if comment_path is None:
            # 본 글이 없으면 댓글 경로 자체가 없어야 한다
            return Inference(None)
        post_id = comment_path["properties"]["post_id"]["enum"][0]
        return Inference(
            {
                "action_kind": "observe",
                "intent": "피드를 훑다가 아는 얼굴의 글에 마음이 멈춘다",
                "target_actor_id": None,
                "location_id": None,
                "params": {},
                "decision_trace": {"trace_id": f"c-{actor_id}-{tick}", "tier": tier},
                "comment": {"post_id": post_id, "text": "새 글 반갑다. 그 시작, 응원할게!"},
            }
        )

    async def converse(self, bundle, *, tier, actor_id, tick):
        return Inference(None)

    async def reflect(self, *args, **kwargs):
        return Inference(None)


async def test_moved_actor_leaves_a_comment_through_llm_decide(conn, redis):
    """③ 액터 답글: 본 글이 decide 컨텍스트·스키마에 열리고, 댓글이 이벤트로 남는다."""
    reader, writer = make_reader(), make_writer()
    mailbox = Mailbox(redis)
    await warm_edge(redis, READER, WRITER)
    post = feed_post()
    await mailbox.push(WORLD, READER, post)

    ai = _CommentingAi()
    phases = ActorPhases(
        [reader, writer],
        ai=ai,
        memory=WorkingMemory(redis),
        mailbox=mailbox,
        emotion=EmotionAdapter(redis),
        relationship=RelationshipAdapter(redis),
    )
    # writer는 이번 tick에 결정하지 않는다 — reader의 댓글만 관측한다
    phases._lods[WRITER] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    await run_tick(conn, phases, CLOCK, WORLD, tick=5, head=0)

    # 본 글이 decide 컨텍스트에 놓였다 (제목 + 작성자 이름)
    reader_bundle = next(b for b in ai.bundles if "피드에서 본 글" in b)
    assert "글쓴이" in reader_bundle and "이야기를 꺼내다" in reader_bundle

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", READER)]
    [comment] = [e for e in events if e["type"] == "actor.message.sent"]
    assert comment["payload"]["channel"] == "comment"
    assert comment["payload"]["target_actor_id"] == WRITER  # 글 작성자에게
    assert comment["payload"]["target_player_id"] is None
    assert comment["payload"]["post_id"] == post["event_id"]
    assert comment["payload"]["in_reply_to"] == post["event_id"]  # 최상위 댓글
    assert comment["causation_id"] == post["event_id"]
    assert comment["correlation_id"] == post["correlation_id"]  # 서사 사슬 승계

    # 댓글은 라우터가 글 작성자에게 배달할 수 있는 모양이다 (깊이 0 → 1)
    assert comment_targets(comment) == [WRITER]

    # 행동 이벤트에는 comment 키가 새지 않았다 (스키마 오염 금지)
    [action] = [e for e in events if e["type"] == "actor.action.performed"]
    assert "comment" not in action["payload"]

    # 자기 댓글이 작업 기억에 남았다 — 다음 결정의 컨텍스트
    recent = await WorkingMemory(redis).recent(WORLD, READER)
    assert any("댓글을 남겼다" in m and "응원할게" in m for m in recent)


async def test_comment_cooldown_closes_the_window(conn, redis):
    """자발 댓글 쿨다운 — 방금 댓글 단 액터는 기회 창이 닫힌다 (수다의 예산).

    댓글마다 글 작성자의 답장(converse)이 따라오므로 이 창이 곧 LLM 비용의
    예산이다 (2026-07-17 과열 관측: tick당 댓글 15건, GPU 96%). 창이 닫힌
    동안의 본 글은 소진되지 않고 다음 기회를 기다린다.
    """
    from lf_actor.rhythm import default_params

    reader, writer = make_reader(), make_writer()
    mailbox = Mailbox(redis)
    await warm_edge(redis, READER, WRITER)
    ai = _CommentingAi()
    phases = ActorPhases(
        [reader, writer], ai=ai, memory=WorkingMemory(redis), mailbox=mailbox,
        emotion=EmotionAdapter(redis), relationship=RelationshipAdapter(redis),
    )
    phases._lods[WRITER] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    # 배달은 관심이 아니라 시계를 안 되돌린다 — 검증 구간 내내 Hot이도록 관심 기준점만 당겨둔다
    phases._lods[READER] = ActorLod(tier=Tier.HOT, last_interest_tick=5)

    post1 = feed_post()
    await mailbox.push(WORLD, READER, post1)
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=5, head=0)  # 첫 댓글

    post2 = feed_post()
    await mailbox.push(WORLD, READER, post2)
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=6, head=head)  # 쿨다운 — 침묵

    cooldown = int(default_params()["social"]["comment_cooldown_ticks"])
    await run_tick(conn, phases, CLOCK, WORLD, tick=5 + cooldown, head=head)  # 창 열림

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", READER)]
    comments = [e for e in events if e["type"] == "actor.message.sent"]
    # 쿨다운 tick의 post2는 소진되지 않고 살아남아 창이 열리는 tick에 댓글이 된다
    assert [c["payload"]["post_id"] for c in comments] == [
        post1["event_id"], post2["event_id"]
    ]


async def test_rule_fallback_never_comments(conn, redis):
    """규칙 폴백은 댓글을 만들지 않는다 — 자발 댓글은 LLM의 몫 (dev 결정성)."""
    reader, writer = make_reader(), make_writer()
    mailbox = Mailbox(redis)
    await mailbox.push(WORLD, READER, feed_post())

    phases = ActorPhases(
        [reader, writer], ai=_SilentAi(), memory=WorkingMemory(redis), mailbox=mailbox,
    )
    phases._lods[WRITER] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    await run_tick(conn, phases, CLOCK, WORLD, tick=5, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", READER)]
    assert not [e for e in events if e["type"] == "actor.message.sent"]
    # 행동은 규칙 폴백으로 남는다 — 세계는 멈추지 않는다
    assert [e for e in events if e["type"] == "actor.action.performed"]


async def test_feed_delivery_is_not_attention(conn, redis):
    """이웃의 글 배달은 '나를 향한 관심'이 아니다 — 강등 시계는 계속 간다.

    배달마다 touch로 타이머가 리셋되면 활발한 세계에선 몇 tick마다 배달이
    와서 전원이 promote 없이도 상시 Hot을 유지한다 (2026-07-18 과열 관측:
    수정 ①(댓글 promote 제거) 후에도 GPU 96% 지속의 남은 누수). 글 자체는
    기억·자발 댓글의 재료로 온전히 소비된다.
    """
    reader, writer = make_reader(), make_writer()
    mailbox = Mailbox(redis)
    phases = ActorPhases(
        [reader, writer], ai=_SilentAi(), memory=WorkingMemory(redis), mailbox=mailbox,
    )
    phases._lods[READER] = ActorLod(tier=Tier.HOT, last_interest_tick=0)
    phases._lods[WRITER] = ActorLod(tier=Tier.COLD, last_interest_tick=0)

    head = 0
    for tick in range(5, 16):  # 매 tick 이웃의 글이 배달돼도
        await mailbox.push(WORLD, READER, feed_post())
        head = await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=head)

    # 관심(tick 0) 기준 유휴가 쌓여 강등됐다 — 배달은 시계를 되돌리지 못했다
    assert phases._lods[READER].tier is not Tier.HOT
    # 글은 소비됐다 — 기억에 남아 다음 결정의 재료가 된다
    recent = await WorkingMemory(redis).recent(WORLD, READER)
    assert any("피드에서" in m for m in recent)


def actor_comment(post: dict, commenter: str = READER) -> dict:
    """reader가 writer의 글에 남긴 최상위 댓글 — 라우터가 작성자에게 배달한 봉투."""
    comment_id = new_ulid()
    return {
        "event_id": comment_id, "stream": "actor", "type": "actor.message.sent",
        "schema_version": 1, "world_id": WORLD, "actor_id": commenter, "tick": 6,
        "occurred_at": "2026-03-01T00:24:00Z", "causation_id": post["event_id"],
        "correlation_id": post["correlation_id"],
        "payload": {
            "channel": "comment",
            "target_player_id": None,
            "target_actor_id": post["actor_id"],
            "text": "새 글 반갑다. 그 시작, 응원할게!",
            "post_id": post["event_id"],
            "in_reply_to": post["event_id"],
        },
    }


async def test_post_author_replies_exactly_once_without_waking(conn, redis):
    """②③ 작성자 답글 의무: 댓글을 받은 작성자는 반드시 한 번 답한다 — Hot 승격 없이.

    답글 경로는 due 무관이라 Hot이 필요 없다. 댓글→promote는 자기 강화 루프였다:
    포스트→댓글→작성자 Hot→매 tick 행동→포스트… 전원이 상시 Hot이 되어 tick이
    LLM 큐에 눌렸다 (2026-07-17 라이브 관측, GPU 96%). 액터 댓글은 touch만 —
    관심은 이어지되 비용은 LOD가 정한다. 플레이어 개입의 promote는 불변(plan/02).

    답글은 댓글 작성자를 향하고 in_reply_to가 댓글 event_id다 — post_id와 달라
    라우터가 다시 돌리지 않는다 (깊이 1 종결). LLM이 없어도(규칙 폴백) 답한다.
    """
    reader, writer = make_reader(), make_writer()
    mailbox = Mailbox(redis)
    post = feed_post()
    comment = actor_comment(post)
    await mailbox.push(WORLD, WRITER, comment)

    phases = ActorPhases(
        [reader, writer], ai=_SilentAi(), memory=WorkingMemory(redis), mailbox=mailbox,
    )
    phases._lods[WRITER] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    phases._lods[READER] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    await run_tick(conn, phases, CLOCK, WORLD, tick=8, head=0)

    # 액터 댓글은 관심 신호(touch)일 뿐 — 잠든 작성자를 깨우지 않는다 (비용 정책)
    assert phases._lods[WRITER].tier is Tier.COLD
    assert phases._lods[WRITER].last_interest_tick == 8  # 강등 타이머는 리셋됐다

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", WRITER)]
    [reply] = [e for e in events if e["type"] == "actor.message.sent"]
    assert reply["payload"]["channel"] == "comment"
    assert reply["payload"]["target_actor_id"] == READER  # 댓글 작성자에게
    assert reply["payload"]["target_player_id"] is None
    assert reply["payload"]["post_id"] == post["event_id"]
    assert reply["payload"]["in_reply_to"] == comment["event_id"]  # 답글의 좌표
    assert reply["causation_id"] == comment["event_id"]
    assert reply["correlation_id"] == post["correlation_id"]
    assert reply["payload"]["text"]  # 규칙 폴백이라도 반드시 답한다

    # 루프 차단 — 작성자의 답글은 라우터가 아무에게도 배달하지 않는다 (깊이 1 끝)
    assert comment_targets(reply) == []

    # 댓글 지각과 자기 답글이 기억에 남았다
    recent = await WorkingMemory(redis).recent(WORLD, WRITER)
    assert any("읽는이" in m and "댓글을 남겼다" in m for m in recent)
    assert any("답했다" in m for m in recent)


# --- 규칙 1c: 오간 말도 관계다 — 액터끼리의 댓글·답장이 관계 엣지로 응고된다 ----


async def test_actor_comment_consolidates_both_sides_of_the_exchange(conn, redis):
    """규칙 1c: 받은 댓글(incoming)과 적재 확정된 내 답글(outgoing)이 수신자→
    발신자 엣지에 응고되고, 교환이 쌓이면 relationship.state.changed로 발행된다.

    실측 공백의 재현: 소셜 댓글 1,500+건에 액터↔액터 간선 0 — 댓글은
    action.performed가 아니고(규칙 1b 밖), inbox 규칙(1a)은 player.*만 걸렀다.
    """
    reader, writer = make_reader(), make_writer()
    mailbox = Mailbox(redis)
    phases = ActorPhases(
        [reader, writer], ai=_SilentAi(), memory=WorkingMemory(redis), mailbox=mailbox,
        relationship=RelationshipAdapter(redis),
    )

    post = feed_post()
    comment = actor_comment(post)
    await mailbox.push(WORLD, WRITER, comment)
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=8, head=0)

    edge_key = f"{WRITER}|{READER}"
    events = [s.envelope for s in await read_stream(conn, WORLD, "relationship", edge_key)]
    assert events, "받은 댓글이 관계 엣지를 만들었어야 한다"
    milestone = events[0]
    assert milestone["type"] == "relationship.milestone.reached"
    assert milestone["payload"]["milestone"] == "first_met"
    assert milestone["causation_id"] == comment["event_id"]  # 원인은 받은 댓글 봉투
    assert "오간 말" in milestone["payload"]["note"]

    # incoming(받은 댓글) + outgoing(적재 확정된 내 답글)이 한 엣지에 쌓였다 —
    # trust는 감쇠가 없어 정확히 두 몫의 합이다
    effects = relationship_params()["interaction_effects"]["actor.message.sent"]
    state = await RelationshipAdapter(redis).load(WORLD, WRITER, READER)
    assert state is not None
    assert state.dimensions["trust"] == pytest.approx(
        effects["incoming"]["trust"] + effects["outgoing"]["trust"]
    )

    # 한 번의 교환은 침묵한다 (발행 임계 미달) — 일상이 곧장 세계 기록이면 소음이다
    assert not [e for e in events if e["type"] == "relationship.state.changed"]

    # 교환이 쌓이면 임계를 넘어 세계에 기록된다 — 그래프 간선의 재료
    post2 = feed_post(title="글쓴이, 두 번째 이야기")
    await mailbox.push(WORLD, WRITER, actor_comment(post2))
    await run_tick(conn, phases, CLOCK, WORLD, tick=9, head=head)

    events = [s.envelope for s in await read_stream(conn, WORLD, "relationship", edge_key)]
    changed = [e for e in events if e["type"] == "relationship.state.changed"]
    assert changed, "쌓인 오간 말이 relationship.state.changed로 발행됐어야 한다"
    payload = changed[0]["payload"]
    assert payload["deltas"]["trust"] > 0
    assert "actor.message.sent" not in payload["reason"]  # reason 계약 — 사람 문장


async def test_spontaneous_comment_records_outgoing_from_stored_envelope(conn, redis):
    """규칙 1c 송신측 — 자발 댓글이 '적재 확정된 봉투'를 원인으로 발신자→작성자
    엣지를 만든다. 수신자 쪽 엣지는 여기서 만들지 않는다: 받은 쪽 마음은
    배달·지각(다음 tick의 inbox)이 바꾼다 — 규칙 1b(양방향)와 다른 결이다.
    """
    reader, writer = make_reader(), make_writer()
    mailbox = Mailbox(redis)
    post = feed_post()
    await mailbox.push(WORLD, READER, post)

    phases = ActorPhases(
        [reader, writer], ai=_CommentingAi(), memory=WorkingMemory(redis), mailbox=mailbox,
        relationship=RelationshipAdapter(redis),
    )
    phases._lods[WRITER] = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    await run_tick(conn, phases, CLOCK, WORLD, tick=5, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", READER)]
    [comment] = [e for e in events if e["type"] == "actor.message.sent"]

    rel_events = [
        s.envelope
        for s in await read_stream(conn, WORLD, "relationship", f"{READER}|{WRITER}")
    ]
    assert rel_events, "적재 확정된 댓글이 발신자 엣지를 만들었어야 한다"
    milestone = rel_events[0]
    assert milestone["payload"]["milestone"] == "first_met"
    assert milestone["causation_id"] == comment["event_id"]  # 적재 확정 봉투가 원인
    assert milestone["correlation_id"] == post["correlation_id"]  # 글의 사슬 승계

    effects = relationship_params()["interaction_effects"]["actor.message.sent"]
    state = await RelationshipAdapter(redis).load(WORLD, READER, WRITER)
    assert state is not None
    assert state.dimensions["trust"] == pytest.approx(effects["outgoing"]["trust"])

    # 받은 쪽 엣지는 아직 없다 — 지각 전의 말은 상대의 마음을 못 바꾼다
    assert await RelationshipAdapter(redis).load(WORLD, WRITER, READER) is None


def player_dm(text: str = "기사 잘 읽고 있어요") -> dict:
    """플레이어 개입 봉투 — 규칙 1a의 재료이자 규칙 1c 경계 검증의 대조군."""
    event_id = new_ulid()
    return {
        "event_id": event_id, "stream": "player", "type": "player.dm.sent",
        "schema_version": 1, "world_id": WORLD, "actor_id": None, "tick": 7,
        "occurred_at": "2026-03-01T00:28:00Z", "causation_id": None,
        "correlation_id": event_id,
        "payload": {"player_id": "p_probe_fan", "target_actor_id": WRITER, "text": text},
    }


async def test_player_reply_stays_outside_rule_1c(conn, redis):
    """경계 — 플레이어 대상 응답(target_player_id)은 규칙 1c 밖이다.

    액터→플레이어 엣지는 규칙 1a(받은 개입)만 만든다: 내 답장까지 한 몫을
    얹으면 플레이어 관계가 이중 응고된다 (그 응고 경로는 이미 있다).
    """
    reader, writer = make_reader(), make_writer()
    mailbox = Mailbox(redis)
    dm = player_dm()
    await mailbox.push(WORLD, WRITER, dm)

    phases = ActorPhases(
        [reader, writer], ai=_SilentAi(), memory=WorkingMemory(redis), mailbox=mailbox,
        relationship=RelationshipAdapter(redis),
    )
    await run_tick(conn, phases, CLOCK, WORLD, tick=7, head=0)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", WRITER)]
    [reply] = [e for e in events if e["type"] == "actor.message.sent"]
    assert reply["payload"]["target_player_id"] == "p_probe_fan"

    edge_key = f"{WRITER}|p_probe_fan"
    rel_events = [s.envelope for s in await read_stream(conn, WORLD, "relationship", edge_key)]
    assert rel_events, "규칙 1a — 받은 DM은 여전히 엣지를 만든다"
    assert all(e["causation_id"] == dm["event_id"] for e in rel_events)  # 답장은 원인이 아니다

    state = await RelationshipAdapter(redis).load(WORLD, WRITER, "p_probe_fan")
    assert state is not None
    assert state.dimensions["trust"] == pytest.approx(
        relationship_params()["interaction_effects"]["player.dm.sent"]["incoming"]["trust"]
    )


async def test_unstored_comment_never_becomes_relationship(conn, redis, monkeypatch):
    """적재가 실패한 말은 관계가 되지 않는다 — 조용한 유실 금지의 역방향.

    관계의 재료는 '적재 확정 봉투'뿐이다: 의도(_pending_comments)를 재료로
    쓰면 유실된 댓글이 유령 엣지를 남긴다.
    """
    import lf_actor.phases as phases_module

    reader, writer = make_reader(), make_writer()
    mailbox = Mailbox(redis)
    await mailbox.push(WORLD, READER, feed_post())
    phases = ActorPhases(
        [reader, writer], ai=_CommentingAi(), memory=WorkingMemory(redis), mailbox=mailbox,
        relationship=RelationshipAdapter(redis),
    )
    phases._lods[WRITER] = ActorLod(tier=Tier.COLD, last_interest_tick=0)

    real_append = phases_module.append

    async def failing_append(conn_, principal, events, *, expected_head):
        if any(e.type == "actor.message.sent" for e in events):
            raise RuntimeError("적재 실패 주입 — CAS 충돌의 재현")
        return await real_append(conn_, principal, events, expected_head=expected_head)

    monkeypatch.setattr(phases_module, "append", failing_append)
    with pytest.raises(RuntimeError):
        await run_tick(conn, phases, CLOCK, WORLD, tick=5, head=0)

    # 실패한 댓글은 관계의 재료가 아니다 — 응고 대기도, 엣지도 없다
    assert phases._resolved_messages == []
    assert await RelationshipAdapter(redis).load(WORLD, READER, WRITER) is None


async def test_feed_post_from_stranger_leaves_memory_but_no_emotion(conn, redis):
    """관계 없는 작성자의 글 — 기억엔 남지만 마음은 흔들리지 않는다."""
    reader, writer = make_reader(), make_writer()
    mailbox = Mailbox(redis)
    await mailbox.push(WORLD, READER, feed_post())

    phases = ActorPhases(
        [reader, writer],
        ai=_SilentAi(),
        memory=WorkingMemory(redis),
        mailbox=mailbox,
        emotion=EmotionAdapter(redis),
        relationship=RelationshipAdapter(redis),
    )
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    recent = await WorkingMemory(redis).recent(WORLD, READER)
    assert any("피드에서" in m for m in recent)
    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", READER)]
    assert not [e for e in events if e["type"] == "actor.emotion.shifted"]
