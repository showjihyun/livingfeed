"""pg-projector 검증 — 봉투→행 변환(순수) + read 테이블 적용(PG 게이트).

멱등성(계약 1)과 재구축(계약 3)이 검증의 중심이다:
같은 봉투 재적용 = 무변화, 신념은 자리 upsert + ULID 순서 가드.
"""

import pytest

from lf_projector.pg_read import (
    NO_ABOUT,
    ReadStore,
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


def test_message_params_rejects_non_conversation():
    with pytest.raises(KeyError):
        message_params(sample("actor.action.performed"))


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


async def test_drop_supports_rebuild(pg):
    store = ReadStore(pg)
    await store.ensure()
    await store.apply(sample("actor.memory.consolidated"))
    await store.drop()
    await store.ensure()  # 재구축 시작점 — 비어 있어야 한다
    count = await (await pg.execute("SELECT count(*) FROM read.actor_episodes")).fetchone()
    assert count == (0,)
