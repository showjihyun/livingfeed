"""pg-projector 검증 — 봉투→행 변환(순수) + read 테이블 적용(PG 게이트).

멱등성(계약 1)과 재구축(계약 3)이 검증의 중심이다:
같은 봉투 재적용 = 무변화, 신념은 자리 upsert + ULID 순서 가드.
"""

import pytest
from lf_projector.pg_read import (
    NO_ABOUT,
    ReadStore,
    StaleReadSchema,
    arc_params,
    belief_params,
    episode_params,
    message_params,
)

from .conftest import sample


def test_episode_params_flattens_envelope():
    params = episode_params(sample("actor.memory.consolidated"))
    assert params[0] == "01JZK7Q3W0000000000000000S"  # event_id가 PK
    assert params[1:4] == ("w_main", "a_aria_kim", 44)
    assert "지지해줬다" in params[5]
    assert params[6] == 0.62


def test_belief_params_normalizes_null_about():
    envelope = sample("actor.belief.formed")
    envelope["payload"]["about_id"] = None
    params = belief_params(envelope)
    assert params[3] == NO_ABOUT  # null은 PK에 참여할 수 있는 자리 표현으로


def test_message_params_normalizes_three_directions():
    reply = message_params(sample("actor.message.sent"))
    outgoing_dm = message_params(sample("player.dm.sent"))
    comment = message_params(sample("player.comment.posted"))
    # (event_id, world, channel, player, actor, sender, ...)
    assert reply[2:6] == ("dm", "p_observer_0417", "a_aria_kim", "actor")
    assert outgoing_dm[2:6] == ("dm", "p_observer_0417", "a_aria_kim", "player")
    assert comment[2:6] == ("comment", "p_observer_0417", "a_aria_kim", "player")
    assert comment[7] == "01JZK7Q3W0000000000000000F"  # post_id 보존
    # in_reply_to: 액터는 페이로드 보존(답장 판별 근거), 플레이어는 항상 최상위(null)
    assert reply[8] == "01JZK7Q3W0000000000000000G"
    assert comment[8] is None


def test_message_params_actor_comment_counterpart_is_the_actor():
    # 액터→액터 댓글 (소셜 루프) — target_player_id가 null, 상대는 target_actor_id
    envelope = sample("actor.message.sent")
    envelope["payload"].update({
        "channel": "comment",
        "target_player_id": None,
        "target_actor_id": "a_junho_park",
        "post_id": "01JZK7Q3W0000000000000000F",
    })
    params = message_params(envelope)
    assert params[2:6] == ("comment", "a_junho_park", "a_aria_kim", "actor")


def test_message_params_rejects_non_conversation():
    with pytest.raises(KeyError):
        message_params(sample("actor.action.performed"))


def test_arc_params_keys_on_payload_target():
    # 아크는 Director 제어 신호 — 봉투 actor_id가 null이라 대상은 payload에서 온다
    envelope = sample("system.director.arc_planned")
    assert envelope["actor_id"] is None
    params = arc_params(envelope)
    assert params[1] == "a_minji_kim"  # payload.target_actor_id가 행의 actor_id
    assert params[2] == "settling"
    assert "이직" in params[3]


async def test_apply_is_idempotent(pg):
    store = ReadStore(pg)
    await store.ensure()
    await store.ensure()  # 멱등 DDL

    for name in ("actor.memory.consolidated", "actor.message.sent", "player.dm.sent"):
        assert await store.apply(sample(name))
        assert await store.apply(sample(name))  # 재전달 — 무변화여야 한다

    counts = await (await pg.execute(
        "SELECT (SELECT count(*) FROM read.actor_episodes),"
        "       (SELECT count(*) FROM read.messages)"
    )).fetchone()
    assert counts == (1, 2)

    # 대화는 방향이 정규화되어 한 대화창 질의로 읽힌다
    rows = await (await pg.execute(
        "SELECT sender, text FROM read.messages"
        " WHERE world_id = 'w_main' AND player_id = 'p_observer_0417'"
        "   AND actor_id = 'a_aria_kim' ORDER BY event_id"
    )).fetchall()
    assert [r[0] for r in rows] == ["player", "actor"]


async def test_comment_thread_projects_by_post_and_replays_idempotently(pg):
    """댓글 영속화의 핵심 계약 — post_id로 조회 가능, 리플레이(재적용)는 무변화.

    플레이어 댓글·액터 답글(대상=플레이어)·액터→액터 소셜 댓글이 전부
    한 포스트의 스레드로 모인다 (도파민 루프의 되읽기 원천).
    """
    store = ReadStore(pg)
    await store.ensure()
    post_id = "01JZK7Q3W0000000000000000F"
    player_comment = sample("player.comment.posted")  # …H: 최상위, post F
    reply = sample("actor.message.sent")
    base_id = reply["event_id"][:-1]
    # …M: 아리의 답글 (플레이어 댓글 …H에 대한 depth-1 답장)
    actor_reply = dict(
        reply, event_id=base_id + "M",
        payload=dict(reply["payload"], channel="comment", post_id=post_id,
                     in_reply_to=player_comment["event_id"]),
    )
    # …N: 준호의 액터→액터 최상위 댓글 (소셜 루프 — in_reply_to == post_id)
    social = dict(
        reply, event_id=base_id + "N", actor_id="a_junho_park",
        payload={"channel": "comment", "target_player_id": None,
                 "target_actor_id": "a_aria_kim", "text": "소셜 루프",
                 "post_id": post_id, "in_reply_to": post_id},
    )
    for envelope in (player_comment, actor_reply, social):
        assert await store.apply(envelope)
        assert await store.apply(envelope)  # 재전달/리플레이 — 무변화여야 한다

    rows = await (await pg.execute(
        "SELECT sender, in_reply_to FROM read.messages"
        " WHERE world_id = 'w_main' AND channel = 'comment' AND post_id = %s"
        " ORDER BY event_id",
        (post_id,),
    )).fetchall()
    assert [r[0] for r in rows] == ["player", "actor", "actor"]  # 시간순 스레드
    assert [r[1] for r in rows] == [None, player_comment["event_id"], post_id]

    # 다른 포스트의 스레드는 비어 있다 — post_id가 조회 키다
    other = await (await pg.execute(
        "SELECT count(*) FROM read.messages"
        " WHERE world_id = 'w_main' AND channel = 'comment' AND post_id = %s",
        ("0" * 26,),
    )).fetchone()
    assert other == (0,)


async def test_identity_projects_and_upserts_forward(pg):
    store = ReadStore(pg)
    await store.ensure()
    assert await store.apply(sample("actor.identity.declared"))
    assert await store.apply(sample("actor.identity.declared"))  # 재선언 — 무변화

    # 나중 선언(더 큰 ULID)은 갱신, 과거 ULID는 무시 (신념과 같은 순서 가드)
    newer = sample("actor.identity.declared")
    newer["event_id"] = newer["event_id"][:-1] + "Z"
    newer["payload"]["name"] = "김아리(수정)"
    await store.apply(newer)
    stale = sample("actor.identity.declared")
    stale["event_id"] = "0" * 26
    stale["payload"]["name"] = "무시됨"
    await store.apply(stale)

    row = await (await pg.execute(
        "SELECT name, archetype, goals FROM read.actors"
        " WHERE world_id = 'w_main' AND actor_id = 'a_aria_kim'"
    )).fetchone()
    assert row[0] == "김아리(수정)"
    assert row[1] == "ambitious_journalist"
    assert any(g["priority"] == 0.9 for g in row[2])  # jsonb → list[dict]


async def test_arc_projects_and_upserts_forward(pg):
    """아크는 (world, actor) 자리 upsert — 다음 계획이 덮어쓴다 (ArcStore와 동일 규약)."""
    store = ReadStore(pg)
    await store.ensure()
    first = sample("system.director.arc_planned")
    assert await store.apply(first)
    assert await store.apply(first)  # 재전달 — 무변화

    # 다음 시즌 계획(더 큰 ULID)이 자리를 덮어쓴다
    newer = sample("system.director.arc_planned")
    newer["event_id"] = first["event_id"][:-1] + "Z"
    newer["payload"]["stage"] = "prime"
    newer["payload"]["intention"] = "결단 이후 — 새 자리에서 증명한다"
    await store.apply(newer)
    # 순서 뒤집힌 과거 계획은 무시된다 (ULID 가드)
    stale = sample("system.director.arc_planned")
    stale["event_id"] = "0" * 26
    stale["payload"]["stage"] = "student"
    await store.apply(stale)

    [row] = await (await pg.execute(
        "SELECT actor_id, stage, intention FROM read.actor_arcs WHERE world_id = 'w_main'"
    )).fetchall()
    assert row[0] == "a_minji_kim"  # 자리 하나 — 이력이 아니라 현재 아크
    assert row[1] == "prime"
    assert "증명한다" in row[2]

    # 연대기는 append-only — 같은 이벤트 재전달만 걸러지고 장의 흐름이 전부 남는다
    history = await (await pg.execute(
        "SELECT stage FROM read.actor_arc_history"
        " WHERE world_id = 'w_main' AND actor_id = 'a_minji_kim' ORDER BY event_id"
    )).fetchall()
    assert [h[0] for h in history] == ["student", "settling", "prime"]  # stale도 이력이다


async def test_unknown_type_is_not_projected(pg):
    store = ReadStore(pg)
    await store.ensure()
    assert not await store.apply(sample("actor.action.performed"))  # 전방 호환 무시


async def test_belief_slot_updates_only_forward(pg):
    store = ReadStore(pg)
    await store.ensure()
    first = sample("actor.belief.formed")
    await store.apply(first)

    # 같은 자리의 나중 발행(더 큰 ULID) — 갱신되고 revisions가 자란다
    newer = sample("actor.belief.formed")
    newer["event_id"] = first["event_id"][:-1] + "Z"
    newer["payload"]["confidence"] = 0.91
    await store.apply(newer)

    # 순서 뒤집힌 재전달(과거 ULID) — 조용히 무시된다
    stale = sample("actor.belief.formed")
    stale["event_id"] = "0" * 26
    stale["payload"]["confidence"] = 0.11
    await store.apply(stale)

    row = await (await pg.execute(
        "SELECT confidence, revisions, event_id FROM read.actor_beliefs"
        " WHERE world_id = 'w_main' AND actor_id = 'a_aria_kim'"
    )).fetchone()
    assert row[0] == pytest.approx(0.91)
    assert row[1] == 2
    assert row[2] == newer["event_id"]


async def test_provenance_reaches_the_read_model(pg):
    """출처가 프로젝션까지 살아 도착한다 — 여기서 끊기면 화면이 기억과 짐작을 못 가른다.

    (ADR-021 §1 제품 표면: 신념은 규칙이 읽어낸 패턴, 기억은 실제 사건에서 인출)
    """
    store = ReadStore(pg)
    await store.ensure()
    await store.apply(sample("actor.belief.formed"))
    await store.apply(sample("actor.memory.consolidated"))

    belief = await (await pg.execute(
        "SELECT provenance_kind FROM read.actor_beliefs WHERE world_id = 'w_main'"
    )).fetchone()
    episode = await (await pg.execute(
        "SELECT provenance_kind FROM read.actor_episodes WHERE world_id = 'w_main'"
    )).fetchone()
    assert belief == ("derived",)
    assert episode == ("recalled",)


async def test_provenance_free_envelope_projects_as_unknown(pg):
    """ADR-021 이전 적재분을 리플레이해도 프로젝션이 멈추지 않는다 — 미상은 미상으로."""
    store = ReadStore(pg)
    await store.ensure()
    legacy = sample("actor.memory.consolidated")
    del legacy["provenance"]
    await store.apply(legacy)

    row = await (await pg.execute(
        "SELECT provenance_kind FROM read.actor_episodes WHERE world_id = 'w_main'"
    )).fetchone()
    assert row == ("unknown",)


async def test_stale_read_schema_fails_loudly(pg):
    """구 스키마 위에서 도는 프로젝터는 첫 이벤트가 아니라 기동에서 멈춘다.

    CREATE TABLE IF NOT EXISTS는 이미 있는 표를 손대지 않으므로, 이 가드가 없으면
    ensure()가 조용히 통과하고 한참 뒤 UndefinedColumn으로 터진다 (ADR-003 계약 3).
    """
    store = ReadStore(pg)
    await store.ensure()
    await pg.execute("ALTER TABLE read.actor_beliefs DROP COLUMN provenance_kind")

    with pytest.raises(StaleReadSchema, match="--rebuild"):
        await store.ensure()

    # 규약대로 재구축하면 풀린다 — 마이그레이션이 아니라 폐기·재생성이다
    await store.drop()
    await store.ensure()


async def test_drop_supports_rebuild(pg):
    store = ReadStore(pg)
    await store.ensure()
    await store.apply(sample("actor.memory.consolidated"))
    await store.drop()
    await store.ensure()  # 재구축 시작점 — 비어 있어야 한다
    count = await (await pg.execute("SELECT count(*) FROM read.actor_episodes")).fetchone()
    assert count == (0,)
