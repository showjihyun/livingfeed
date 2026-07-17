"""redis-projector 검증 — 팔로워 인덱스·팬아웃(Redis 게이트) + 순수 변환.

멱등성(계약 1)이 중심이다: ZADD 재기록이 재전달을 흡수하고, 상한이
타임라인을 캡 아래로 유지한다 (ADR-014 리스크 완화).
"""

import json

from lf_projector.timeline import (
    TimelineStore,
    follower_pair,
    receipt_doc,
    reply_to_doc,
    ulid_ms,
)

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


def test_receipt_doc_is_feed_item_shaped():
    """변화 리시트도 등급이 다른 같은 데이터다 — 포스트 doc과 같은 키 집합 (ADR-014).

    도파민 §붕괴 방어: "영향력이 안 보임" — 수치 없는 정성 서사로만 가시화한다.
    """
    from lf_projector.os_index import envelope_to_doc

    doc = receipt_doc(sample("relationship.state.changed"))
    post_doc = envelope_to_doc(sample("feed.post.published"))
    assert set(doc) == set(post_doc)
    assert doc["visibility"] == "private"


def test_receipt_only_for_actor_to_player_direction():
    """액터의 마음이 변한 것만 리시트한다 — 플레이어 자신의 마음은 자명해서 소음이다."""
    base = sample("relationship.state.changed")

    actor_to_actor = dict(base)
    actor_to_actor["payload"] = dict(base["payload"], to_id="a_junho_park")
    assert receipt_doc(actor_to_actor) is None  # 플레이어 무관 — 세계의 일상이다

    player_to_actor = dict(base)
    player_to_actor["payload"] = dict(
        base["payload"], from_id=PLAYER, to_id="a_aria_kim"
    )
    assert receipt_doc(player_to_actor) is None  # from=플레이어 — 자기 마음은 안다


def test_receipt_narrates_dominant_dimension_without_numbers():
    """지배 차원(|delta| 최대)만 정성 문장으로 — 수치 노출은 조작감을 만든다."""
    base = sample("relationship.state.changed")

    trust_up = receipt_doc(base)  # 샘플의 지배 차원은 trust +0.07
    assert "신뢰가 조금 자랐다" in trust_up["body"]
    assert "0.07" not in trust_up["body"] and "0.07" not in trust_up["title"]
    # 엔진이 쓴 사유가 본문의 앞자리를 채운다
    assert trust_up["body"].startswith(base["payload"]["reason"])

    def with_deltas(**deltas):
        env = dict(base)
        zero = dict.fromkeys(base["payload"]["deltas"], 0.0)
        env["payload"] = dict(base["payload"], deltas={**zero, **deltas}, reason="")
        return env

    assert "신뢰에 금이 갔다" in receipt_doc(with_deltas(trust=-0.1))["body"]
    assert "마음의 거리가 가까워졌다" in receipt_doc(with_deltas(intimacy=0.1))["body"]
    assert (
        "마음 한켠에 앙금이 남았다"
        in receipt_doc(with_deltas(resentment=0.2, trust=-0.1))["body"]
    )


def test_receipt_skips_negligible_change():
    """모든 |delta| < 임계(RECEIPT_DELTA_FLOOR)면 생략 — 리시트가 스팸이 되면 안 된다."""
    base = sample("relationship.state.changed")

    def with_uniform_delta(value):
        env = dict(base)
        deltas = dict.fromkeys(base["payload"]["deltas"], value)
        env["payload"] = dict(base["payload"], deltas=deltas)
        return env

    assert receipt_doc(with_uniform_delta(0.02)) is None
    assert receipt_doc(with_uniform_delta(-0.029)) is None
    assert receipt_doc(with_uniform_delta(0.03)) is not None  # 임계는 포함이다


def test_milestone_receipt_labels_known_kind_and_falls_back_on_unknown():
    """마일스톤은 kind별 라벨로 서사화한다 — 모르는 kind는 원문 폴백 (전방 호환)."""
    base = sample("relationship.milestone.reached")

    first_met = receipt_doc(base)
    assert first_met["visibility"] == "private"
    assert "서로를 알게 됐다" in first_met["body"]
    assert first_met["body"].startswith(base["payload"]["note"])

    unknown = dict(base)
    unknown["payload"] = dict(base["payload"], milestone="quantum_entangled", note="")
    assert "quantum_entangled" in receipt_doc(unknown)["body"]


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


async def test_receipt_lands_on_player_timeline_idempotently(redis):
    """리시트는 to=플레이어 단독 배달 — 재전달은 기존 ZADD 패턴이 흡수한다 (계약 1)."""
    store = TimelineStore(redis)
    envelope = sample("relationship.state.changed")

    await store.push_receipt(envelope)
    await store.push_receipt(envelope)  # 재전달 — ZADD가 흡수한다

    raw = await redis.zrange(store.timeline_key(WORLD, PLAYER), 0, -1)
    assert len(raw) == 1
    doc = json.loads(raw[0])
    assert doc["visibility"] == "private"
    assert doc["event_id"] == envelope["event_id"]


async def test_skipped_receipt_leaves_no_timeline(redis):
    """생략된 리시트는 유령 키도 남기지 않는다 — 조용히 통과한다."""
    store = TimelineStore(redis)
    envelope = sample("relationship.state.changed")
    envelope["payload"] = dict(envelope["payload"], to_id="a_junho_park")
    await store.push_receipt(envelope)
    assert [k async for k in redis.scan_iter(match="lf:tl:*")] == []


async def test_actor_comment_is_not_privately_delivered(redis):
    """액터→액터 댓글 (소셜 루프) — 수신 플레이어가 없으니 Private 배달도 없다.

    world 가시성 댓글은 세션 push(gateway)의 몫이다 — None 키 타임라인을 만들지 않는다.
    """
    store = TimelineStore(redis)
    envelope = sample("actor.message.sent")
    envelope["payload"].update({
        "channel": "comment", "target_player_id": None, "target_actor_id": "a_junho_park",
    })
    await store.push_reply(envelope)  # 조용히 통과 — 예외도 유령 키도 없다
    assert [k async for k in redis.scan_iter(match="lf:tl:*")] == []


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


async def test_relationship_handler_registers_follower_and_delivers_receipt(redis):
    """redis-projector의 relationship 적용자는 팔로워 인덱스와 리시트를 함께 처리한다."""
    from lf_projector.config import Config
    from lf_projector.redis_projector import RedisProjector

    store = TimelineStore(redis)
    apply = RedisProjector(
        Config(nats_url="", opensearch_url="", env="test")
    )._apply_relationship(store)

    await apply(sample("relationship.state.changed"))

    assert await store.followers(WORLD, "a_aria_kim") == {PLAYER}
    raw = await redis.zrange(store.timeline_key(WORLD, PLAYER), 0, -1)
    assert [json.loads(r)["source_event_type"] for r in raw] == [
        "relationship.state.changed"
    ]


async def test_drop_all_supports_rebuild(redis):
    store = TimelineStore(redis)
    await store.register_follower(WORLD, "a_aria_kim", PLAYER)
    await store.push_reply(sample("actor.message.sent"))
    await store.drop_all()
    assert await store.followers(WORLD, "a_aria_kim") == set()
    assert await redis.exists(store.timeline_key(WORLD, PLAYER)) == 0
