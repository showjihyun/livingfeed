"""redis-projector 검증 — 팔로워 인덱스·팬아웃(Redis 게이트) + 순수 변환.

멱등성(계약 1)이 중심이다: ZADD 재기록이 재전달을 흡수하고, 상한이
타임라인을 캡 아래로 유지한다 (ADR-014 리스크 완화).
"""

import json

from lf_projector.timeline import TimelineStore, follower_pair, reply_to_doc, ulid_ms

from .conftest import sample

WORLD = "w_main"
PLAYER = "p_observer_0417"


def test_ulid_ms_orders_by_time():
    older, newer = "01JZK7Q3W0000000000000000G", "01JZK7Q3W1000000000000000G"
    assert ulid_ms(older) < ulid_ms(newer)


def test_follower_pair_reads_both_directions():
    assert follower_pair({"from_id": "a_aria_kim", "to_id": PLAYER}) == ("a_aria_kim", PLAYER)
    assert follower_pair({"from_id": PLAYER, "to_id": "a_aria_kim"}) == ("a_aria_kim", PLAYER)
    assert follower_pair({"from_id": "a_aria_kim", "to_id": "a_junho_park"}) is None


def test_reply_doc_is_feed_item_shaped():
    doc = reply_to_doc(sample("actor.message.sent"))
    # 6가지 피드는 등급이 다른 같은 데이터다 — 포스트 doc과 같은 키 집합 (ADR-014)
    from lf_projector.os_index import envelope_to_doc
    post_doc = envelope_to_doc(sample("feed.post.published"))
    assert set(doc) == set(post_doc)
    assert doc["visibility"] == "private"
    assert "힘이 나요" in doc["body"]


async def test_relationship_event_registers_follower(redis):
    store = TimelineStore(redis)
    pair = follower_pair(sample("relationship.milestone.reached")["payload"])
    await store.register_follower(WORLD, *pair)
    assert await store.followers(WORLD, "a_aria_kim") == {PLAYER}


async def test_explicit_unfollow_wins_over_relationship_standin(redis):
    """진짜 팔로우 모델 (ADR-014) — 명시 철회는 관계 stand-in을 이긴다.

    언팔로우한 대상은 관계 엣지가 계속 이벤트를 내도 타임라인에 되살아나지
    않는다. 다시 명시 팔로우하면 거부 마커가 걷힌다 (마지막 선언이 이긴다).
    """
    store = TimelineStore(redis)
    actor = "a_aria_kim"

    # 명시 팔로우 → 인덱스에 오른다
    await store.set_follow(WORLD, actor, PLAYER, True)
    assert await store.followers(WORLD, actor) == {PLAYER}

    # 명시 철회 → 빠지고, 관계 유래 등록도 되살리지 못한다
    await store.set_follow(WORLD, actor, PLAYER, False)
    assert await store.followers(WORLD, actor) == set()
    await store.register_follower(WORLD, actor, PLAYER)  # 관계 stand-in 시도
    assert await store.followers(WORLD, actor) == set()  # 철회는 명시적 의사다

    # 다른 플레이어의 관계 유래 등록은 그대로 동작한다
    await store.register_follower(WORLD, actor, "p_other")
    assert await store.followers(WORLD, actor) == {"p_other"}

    # 재선언 → 마커가 걷히고 되돌아온다
    await store.set_follow(WORLD, actor, PLAYER, True)
    assert await store.followers(WORLD, actor) == {PLAYER, "p_other"}


async def test_fan_out_reaches_participants_followers(redis):
    store = TimelineStore(redis)
    # 참여자(준호)만 아는 플레이어에게도 이 포스트가 실려야 한다
    await store.register_follower(WORLD, "a_junho_park", "p_junho_fan")
    post = sample("feed.post.published")

    assert await store.fan_out_post(post) == 1
    assert await store.fan_out_post(post) == 1  # 재전달 — ZADD가 흡수한다

    raw = await redis.zrange(store.timeline_key(WORLD, "p_junho_fan"), 0, -1)
    assert len(raw) == 1
    assert json.loads(raw[0])["event_id"] == post["event_id"]


async def test_reply_lands_only_on_target_player(redis):
    store = TimelineStore(redis)
    await store.push_reply(sample("actor.message.sent"))
    raw = await redis.zrange(store.timeline_key(WORLD, PLAYER), 0, -1)
    assert json.loads(raw[0])["visibility"] == "private"


async def test_timeline_cap_keeps_newest(redis, monkeypatch):
    monkeypatch.setattr("lf_projector.timeline.TIMELINE_CAP", 3)
    store = TimelineStore(redis)
    base = sample("actor.message.sent")
    for i in range(5):
        await store.push_reply(dict(base, event_id=f"01JZK7Q3W{i}000000000000000A"))

    raw = await redis.zrevrange(store.timeline_key(WORLD, PLAYER), 0, -1)
    kept = [json.loads(r)["event_id"] for r in raw]
    assert len(kept) == 3
    assert kept[0].startswith("01JZK7Q3W4")  # 최신이 남고 과거가 밀려난다


async def test_drop_all_supports_rebuild(redis):
    store = TimelineStore(redis)
    await store.register_follower(WORLD, "a_aria_kim", PLAYER)
    await store.push_reply(sample("actor.message.sent"))
    await store.drop_all()
    assert await store.followers(WORLD, "a_aria_kim") == set()
    assert await redis.exists(store.timeline_key(WORLD, PLAYER)) == 0
