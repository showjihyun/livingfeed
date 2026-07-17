"""액터 소셜 루프 — 피드 순환의 배달 대상 선정과 댓글 깊이 1 차단 (순수 + Redis).

라우터가 feed.post.published를 이웃에게, 액터 댓글을 글 작성자에게 배달한다.
선정은 결정적이다 — 같은 (포스트, 관계 상태)면 같은 이웃 (리플레이 재현성).
"""

from lf_actor.relationship import RelationshipAdapter
from lf_actor.social import FeedFanout, comment_targets, fanout_targets
from lf_relationship import RelationshipState

WORLD = "w_test"
ROSTER = ["a_ana", "a_bora", "a_chul", "a_dana", "a_eun", "a_writer"]
POST_ID = "01JZK7Q3W0000000000000000F"


def feed_envelope(author: str | None, *, visibility: str = "world") -> dict:
    return {
        "event_id": POST_ID,
        "stream": "feed",
        "type": "feed.post.published",
        "schema_version": 1,
        "world_id": WORLD,
        "actor_id": author,
        "tick": 42,
        "occurred_at": "2026-03-01T00:00:00Z",
        "causation_id": "01JZK7Q3W0000000000000000A",
        "correlation_id": "01JZK7Q3W0000000000000000B",
        "payload": {
            "visibility": visibility,
            "title": "글쓴이, 이야기를 꺼내다",
            "body": "요즘 하는 일 이야기",
            "narration_kind": "template",
            "participants": [author] if author else [],
            "community_id": None,
            "location_id": None,
            "drama_score": 0.4,
            "worthiness": 0.5,
            "source_event_type": "actor.action.performed",
            "tags": ["speak"],
            "media": [],
        },
    }


def comment_envelope(
    commenter: str, target: str | None, *, in_reply_to: str, channel: str = "comment"
) -> dict:
    return {
        "event_id": "01JZK7Q3W0000000000000000M",
        "stream": "actor",
        "type": "actor.message.sent",
        "schema_version": 1,
        "world_id": WORLD,
        "actor_id": commenter,
        "tick": 43,
        "occurred_at": "2026-03-01T00:01:00Z",
        "causation_id": POST_ID,
        "correlation_id": "01JZK7Q3W0000000000000000B",
        "payload": {
            "channel": channel,
            "target_player_id": None if target and target.startswith("a_") else target,
            "target_actor_id": target if target and target.startswith("a_") else None,
            "text": "잘 읽었어",
            "post_id": POST_ID,
            "in_reply_to": in_reply_to,
        },
    }


# --- 배달 대상 선정 (순수) ------------------------------------------------------


def test_fanout_picks_top_salience_neighbors_excluding_author():
    saliences = {"a_ana": 0.9, "a_bora": 0.2, "a_chul": 0.5, "a_dana": 0.7, "a_writer": 1.0}
    targets = fanout_targets(
        "a_writer", POST_ID, ROSTER, saliences, limit=3
    )
    assert targets == ["a_ana", "a_dana", "a_chul"]  # salience 내림차순, 작성자 제외


def test_fanout_salience_tie_breaks_deterministically_by_id():
    saliences = {"a_bora": 0.5, "a_ana": 0.5, "a_chul": 0.5}
    targets = fanout_targets("a_writer", POST_ID, ROSTER, saliences, limit=2)
    assert targets == ["a_ana", "a_bora"]  # 동률은 id 순 — 결정적


def test_fanout_without_any_relation_picks_deterministic_neighbors():
    first = fanout_targets("a_writer", POST_ID, ROSTER, {}, limit=3)
    second = fanout_targets("a_writer", POST_ID, ROSTER, {}, limit=3)
    assert first == second  # 결정적 해시 — 같은 포스트면 같은 이웃
    assert len(first) == 3
    assert "a_writer" not in first  # 자기 글은 자기에게 안 간다
    # 다른 포스트는 다른 이웃일 수 있다 — 해시의 소금이 post id다
    other = fanout_targets("a_writer", "01JZK7Q3W0000000000000000Z", ROSTER, {}, limit=3)
    assert set(other) <= set(ROSTER) - {"a_writer"}


def test_fanout_related_neighbors_win_even_if_fewer_than_limit():
    # 관계가 하나라도 있으면 해시 채움 없이 그들에게만 간다 (관계가 곧 이웃이다)
    targets = fanout_targets("a_writer", POST_ID, ROSTER, {"a_eun": 0.1}, limit=3)
    assert targets == ["a_eun"]


# --- 댓글 라우팅 — 깊이 1 차단 (순수) -------------------------------------------


def test_top_level_comment_is_delivered_to_post_author_only():
    comment = comment_envelope("a_bora", "a_writer", in_reply_to=POST_ID)
    assert comment_targets(comment) == ["a_writer"]


def test_author_reply_to_comment_is_not_routed_again():
    # 작성자의 답글: in_reply_to가 댓글 event_id라 post_id와 다르다 — 깊이 1에서 끝
    reply = comment_envelope(
        "a_writer", "a_bora", in_reply_to="01JZK7Q3W0000000000000000M"
    )
    assert comment_targets(reply) == []


def test_player_directed_reply_is_not_routed_to_actors():
    reply = comment_envelope("a_writer", "p_observer_0417", in_reply_to=POST_ID)
    assert comment_targets(reply) == []


def test_dm_is_never_routed_between_actors():
    dm = comment_envelope("a_writer", "a_bora", in_reply_to=POST_ID, channel="dm")
    assert comment_targets(dm) == []


def test_self_targeted_comment_is_dropped():
    weird = comment_envelope("a_writer", "a_writer", in_reply_to=POST_ID)
    assert comment_targets(weird) == []


# --- 라우터 디스패치 (mailbox.route_targets) ------------------------------------


def player_envelope(event_type: str, target: str) -> dict:
    return {
        "event_id": "01JZK7Q3W0000000000000000P",
        "stream": "player", "type": event_type, "schema_version": 1,
        "world_id": WORLD, "actor_id": None, "tick": 0,
        "occurred_at": "2026-03-01T00:00:00Z", "causation_id": None,
        "correlation_id": "01JZK7Q3W0000000000000000P",
        "payload": {"player_id": "p_observer_0417", "target_actor_id": target,
                    "text": "안녕"},
    }


async def test_route_targets_dispatches_by_event_kind(redis):
    from lf_actor.mailbox import route_targets

    await seed_edge(redis, "a_ana", "a_writer", 0.9)
    fanout = FeedFanout(RelationshipAdapter(redis), ROSTER, limit=3)

    # 피드 순환 — 관계 이웃에게
    assert await route_targets(feed_envelope("a_writer"), fanout) == ["a_ana"]
    # 액터 댓글 — 글 작성자에게만 (fanout 없이도 순수 규칙)
    comment = comment_envelope("a_bora", "a_writer", in_reply_to=POST_ID)
    assert await route_targets(comment, fanout) == ["a_writer"]
    # 플레이어 개입 — 기존 단일 대상 규약 그대로
    dm = player_envelope("player.dm.sent", "a_writer")
    assert await route_targets(dm, fanout) == ["a_writer"]
    # fanout 미주입(소셜 루프 꺼짐) — 피드는 돌지 않는다
    assert await route_targets(feed_envelope("a_writer"), None) == []


# --- FeedFanout (Redis 관계 엣지 조회) ------------------------------------------


async def seed_edge(redis, reader: str, author: str, salience: float) -> None:
    adapter = RelationshipAdapter(redis)
    state = RelationshipState(salience=salience, stage="acquaintance")
    await adapter.save(WORLD, reader, author, state)
    await redis.sadd(f"lf:relidx:{WORLD}:{reader}", author)


async def test_feed_fanout_reads_reader_edges_toward_author(redis):
    await seed_edge(redis, "a_ana", "a_writer", 0.9)
    await seed_edge(redis, "a_bora", "a_writer", 0.3)
    await seed_edge(redis, "a_chul", "a_writer", 0.6)
    await seed_edge(redis, "a_dana", "a_writer", 0.1)
    fanout = FeedFanout(RelationshipAdapter(redis), ROSTER, limit=3)

    targets = await fanout.targets(feed_envelope("a_writer"))
    assert targets == ["a_ana", "a_chul", "a_bora"]


async def test_feed_fanout_skips_non_world_and_authorless_posts(redis):
    fanout = FeedFanout(RelationshipAdapter(redis), ROSTER, limit=3)
    assert await fanout.targets(feed_envelope("a_writer", visibility="private")) == []
    assert await fanout.targets(feed_envelope(None)) == []  # 세계 뉴스 — 작성자가 없다


async def test_feed_fanout_gives_new_actor_hashed_neighbors(redis):
    # 아직 아무와도 얽히지 않은 새 인물의 글도 이웃이 생긴다 (결정적 해시)
    fanout = FeedFanout(RelationshipAdapter(redis), ROSTER, limit=3)
    targets = await fanout.targets(feed_envelope("a_writer"))
    assert len(targets) == 3
    assert "a_writer" not in targets
    assert targets == await fanout.targets(feed_envelope("a_writer"))  # 결정적
