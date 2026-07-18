"""루프 헬스 검증 — 봉투 열 → 도파민 루프 서버측 프록시 리포트 (plan/02 §측정).

fold_loop_health는 순수·결정적: 같은 봉투 열 → 같은 리포트 (리플레이 재현).
'반응 확인'(클라이언트 열람)은 서버가 모른다 — 여기서 검증하는 것은 전부
정직한 프록시다: 응답 발생률·지연(이벤트 적재 시각)·재개입·드라마 재생산.
PG 통합은 loop_health가 그 날 창만 읽는지 확인한다 — 플레이어 개입은
tick 0 규약(gateway)이라 tick 창이 아니라 그 날의 벽시계 창으로 골라낸다.
"""

from datetime import UTC, datetime, timedelta

from lf_director.loop_health import fold_loop_health, loop_health
from lf_eventstore import NewEvent, append, current_head

WORLD = "w_test"
BASE = datetime(2026, 7, 17, 9, 0, 0, tzinfo=UTC)
ULID = "01JZK7Q3W000000000000000"  # 24자 + 2자 suffix = 26자 ULID


def when(sec: float) -> str:
    return (BASE + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")


def dm(eid: str, player: str, sec: float) -> dict:
    return {
        "type": "player.dm.sent", "event_id": eid, "occurred_at": when(sec),
        "causation_id": None, "correlation_id": eid,
        "payload": {"player_id": player, "target_actor_id": "a_minji", "text": "x"},
    }


def comment(eid: str, player: str, sec: float, *, post: str) -> dict:
    # gateway 규약: 포스트 개입은 그 포스트의 서사 사슬을 잇는다 (correlation=post_id)
    return {
        "type": "player.comment.posted", "event_id": eid, "occurred_at": when(sec),
        "causation_id": None, "correlation_id": post,
        "payload": {"player_id": player, "target_actor_id": "a_minji",
                    "post_id": post, "text": "x"},
    }


def like(eid: str, player: str, sec: float, *, post: str) -> dict:
    return {
        "type": "player.reaction.added", "event_id": eid, "occurred_at": when(sec),
        "causation_id": None, "correlation_id": post,
        "payload": {"player_id": player, "target_actor_id": "a_minji",
                    "post_id": post, "kind": "like"},
    }


def reply(eid: str, cause: str | None, player: str | None, sec: float) -> dict:
    return {
        "type": "actor.message.sent", "event_id": eid, "occurred_at": when(sec),
        "causation_id": cause, "correlation_id": cause or eid,
        "payload": {"channel": "dm", "target_player_id": player, "text": "y",
                    "post_id": None, "in_reply_to": cause},
    }


def post(eid: str, corr: str, sec: float) -> dict:
    return {
        "type": "feed.post.published", "event_id": eid, "occurred_at": when(sec),
        "causation_id": None, "correlation_id": corr,
        "payload": {"title": "t", "body": "b", "drama_score": 0.5},
    }


def test_fold_measures_response_rate_and_latency():
    """응답률 분모는 dm/댓글(응답 의무 개입)만 — 첫 응답 지연에서 p50/p95."""
    envelopes = [
        dm(ULID + "0A", "p_one", 0),
        comment(ULID + "0B", "p_two", 10, post=ULID + "0P"),
        dm(ULID + "0C", "p_three", 20),           # 무응답
        like(ULID + "0D", "p_four", 30, post=ULID + "0P"),  # 응답 의무 없음 — 분모 밖
        reply(ULID + "1A", ULID + "0A", "p_one", 2),        # 지연 2초
        reply(ULID + "1B", ULID + "0A", "p_one", 50),       # 두 번째 응답 — 첫 응답만 지연
        reply(ULID + "1C", ULID + "0B", "p_two", 40),       # 지연 30초
    ]
    report = fold_loop_health(envelopes)
    assert report["interventions"]["total"] == 4
    assert report["interventions"]["by_type"] == {
        "player.comment.posted": 1, "player.dm.sent": 2, "player.reaction.added": 1,
    }
    assert report["interventions"]["players"] == 4
    r = report["responses"]
    assert r["eligible_interventions"] == 3
    assert r["responded_interventions"] == 2
    assert r["response_rate"] == 0.6667
    assert r["matched_responses"] == 3
    assert r["latency_seconds"] == {"p50": 2.0, "p95": 30.0, "samples": 2}


def test_fold_matches_responses_only_by_causation():
    """causation이 개입을 가리키는 응답만 — 선제 DM(causation 없음)·액터간 댓글은 응답이 아니다."""
    envelopes = [
        dm(ULID + "0A", "p_one", 0),
        reply(ULID + "1A", None, "p_one", 5),           # 선제 DM — 개입 응답 아님
        reply(ULID + "1B", ULID + "0Z", "p_one", 6),    # 창 밖 causation — 매칭 실패
        reply(ULID + "1C", ULID + "0A", None, 7),       # target_player 없음 — 액터간
    ]
    r = fold_loop_health(envelopes)["responses"]
    assert r["matched_responses"] == 0
    assert r["responded_interventions"] == 0
    assert r["response_rate"] == 0.0
    assert r["latency_seconds"] == {"p50": None, "p95": None, "samples": 0}


def test_fold_like_greeting_counts_as_response_but_not_rate():
    """좋아요 첫 인사(reaction 응답)는 매칭 응답·재개입 재료다 — 응답률 분모는 아니다."""
    envelopes = [
        like(ULID + "0A", "p_one", 0, post=ULID + "0P"),
        reply(ULID + "1A", ULID + "0A", "p_one", 3),
    ]
    report = fold_loop_health(envelopes)
    assert report["responses"]["eligible_interventions"] == 0
    assert report["responses"]["response_rate"] is None
    assert report["responses"]["matched_responses"] == 1
    assert report["reengagement"]["responded_players"] == 1


def test_fold_reengagement_counts_only_after_response():
    """응답을 받은 뒤의 개입만 재개입이다 — 응답 이전의 재개입은 루프 재회전이 아니다."""
    envelopes = [
        dm(ULID + "0A", "p_one", 0),
        reply(ULID + "1A", ULID + "0A", "p_one", 10),
        dm(ULID + "0B", "p_one", 20),                  # 응답 이후 — 재개입
        dm(ULID + "0C", "p_two", 0),
        dm(ULID + "0D", "p_two", 5),                   # 응답 이전 — 재개입 아님
        reply(ULID + "1B", ULID + "0C", "p_two", 30),
        dm(ULID + "0E", "p_three", 0),                 # 응답 자체가 없음 — 분모 밖
    ]
    report = fold_loop_health(envelopes)["reengagement"]
    assert report["responded_players"] == 2
    assert report["reengaged_players"] == 1
    assert report["rate"] == 0.5


def test_fold_drama_reproduction_requires_correlation_inheritance():
    """개입 correlation을 승계한 포스트만 2차 사건이다 — 남의 사슬은 세지 않는다."""
    envelopes = [
        dm(ULID + "0A", "p_one", 0),                       # correlation 루트 = 자기 자신
        comment(ULID + "0B", "p_two", 10, post=ULID + "0P"),  # correlation = 포스트 사슬
        post(ULID + "2A", ULID + "0A", 60),                # DM 사슬 승계 — 2차 사건
        post(ULID + "2B", ULID + "0P", 70),                # 댓글 사슬 승계 — 2차 사건
        post(ULID + "2C", ULID + "0Z", 80),                # 무관한 사슬 — 아님
    ]
    report = fold_loop_health(envelopes)["drama_reproduction"]
    assert report["derived_posts"] == 2
    assert report["rate"] == 1.0


def test_fold_zero_interventions_graceful():
    """개입 0의 우아한 영값 — 분모 0은 전부 None, 카운트는 0."""
    empty = fold_loop_health([])
    assert empty["interventions"] == {"total": 0, "by_type": {}, "players": 0}
    assert empty["responses"]["response_rate"] is None
    assert empty["responses"]["latency_seconds"] == {"p50": None, "p95": None, "samples": 0}
    assert empty["reengagement"] == {
        "responded_players": 0, "reengaged_players": 0, "rate": None,
    }
    assert empty["drama_reproduction"] == {"derived_posts": 0, "rate": None}
    # 개입 없는 선제 DM·포스트만 있어도 영값 유지 (분모가 없다)
    stray = fold_loop_health(
        [reply(ULID + "1A", None, "p_one", 0), post(ULID + "2A", ULID + "0Z", 1)]
    )
    assert stray["interventions"]["total"] == 0
    assert stray["drama_reproduction"] == {"derived_posts": 0, "rate": None}


def test_fold_is_order_insensitive_and_deterministic():
    """봉투가 뒤섞여 들어와도 같은 리포트 — 리플레이 재현."""
    envelopes = [
        dm(ULID + "0A", "p_one", 0),
        reply(ULID + "1A", ULID + "0A", "p_one", 2),
        dm(ULID + "0B", "p_one", 20),
        post(ULID + "2A", ULID + "0A", 60),
    ]
    assert fold_loop_health(envelopes) == fold_loop_health(list(reversed(envelopes)))
    assert fold_loop_health(envelopes) == fold_loop_health(envelopes)


async def test_loop_health_reads_only_that_day_window(conn):
    """그 날 창만 접는다 — tick 이벤트로 벽시계 창을 잡고, tick 0 규약의
    플레이어 개입은 occurred_at으로, tick이 실린 응답·포스트는 tick 창으로 고른다."""

    async def seed(principal: str, event: NewEvent) -> None:
        head = await current_head(conn, event.world_id, event.stream, event.stream_key)
        await append(conn, principal, [event], expected_head=head)

    def tick_completed(eid: str, tick: int, sec: float) -> NewEvent:
        stamp = when(sec)
        return NewEvent(
            world_id=WORLD, stream="system", stream_key="tick",
            type="system.tick.completed", tick=tick, event_id=eid,
            occurred_at=BASE + timedelta(seconds=sec),
            payload={"tick": tick, "started_at": stamp, "completed_at": stamp,
                     "duration_ms": 1, "actors_decided": {"hot": 0, "warm": 0, "cold": 0},
                     "events_emitted": 0},
        )

    dm1, dm2 = ULID + "0A", ULID + "0B"
    # day 0 벽시계 창: tick 5(+10s) — day 1은 tick 365(+1000s)부터
    await seed("engine.tick", tick_completed(ULID + "T0", 5, 10))
    await seed("engine.tick", tick_completed(ULID + "T1", 365, 1000))
    # 플레이어 개입 — gateway 규약대로 tick 0, 순서의 진실은 occurred_at/ULID
    await seed("services.gateway", NewEvent(
        world_id=WORLD, stream="player", stream_key="p_one", type="player.dm.sent",
        tick=0, event_id=dm1, occurred_at=BASE + timedelta(seconds=100),
        payload={"player_id": "p_one", "target_actor_id": "a_minji", "text": "hi"},
    ))
    await seed("services.gateway", NewEvent(
        world_id=WORLD, stream="player", stream_key="p_one", type="player.dm.sent",
        tick=0, event_id=dm2, occurred_at=BASE + timedelta(seconds=2000),  # day 1
        payload={"player_id": "p_one", "target_actor_id": "a_minji", "text": "again"},
    ))
    # 응답 — 실제 tick이 실린다 (day 0), causation이 개입을 가리킨다
    await seed("engine.actor", NewEvent(
        world_id=WORLD, stream="actor", stream_key="a_minji", type="actor.message.sent",
        tick=6, actor_id="a_minji", event_id=ULID + "1A",
        occurred_at=BASE + timedelta(seconds=103),
        causation_id=dm1, correlation_id=dm1,
        payload={"channel": "dm", "target_player_id": "p_one", "text": "hey",
                 "post_id": None, "in_reply_to": dm1},
    ))
    # 2차 사건 — 개입 correlation을 승계한 포스트 (day 0)
    await seed("engine.feed", NewEvent(
        world_id=WORLD, stream="feed", stream_key=ULID + "2A", type="feed.post.published",
        tick=7, actor_id="a_minji", event_id=ULID + "2A",
        occurred_at=BASE + timedelta(seconds=110),
        causation_id=ULID + "1A", correlation_id=dm1,
        payload={"visibility": "world", "title": "t", "body": "b",
                 "narration_kind": "template", "participants": ["a_minji"],
                 "community_id": None, "location_id": None, "drama_score": 0.5,
                 "worthiness": 0.5, "source_event_type": "actor.message.sent",
                 "tags": [], "media": []},
    ))

    day0 = await loop_health(conn, WORLD, day=0, interval_ticks=360)
    assert day0["interventions"]["total"] == 1          # dm2는 day 1 — 창 밖
    assert day0["responses"]["responded_interventions"] == 1
    assert day0["responses"]["latency_seconds"] == {"p50": 3.0, "p95": 3.0, "samples": 1}
    assert day0["drama_reproduction"] == {"derived_posts": 1, "rate": 1.0}
    assert day0["tick_range"] == [0, 360]

    day1 = await loop_health(conn, WORLD, day=1, interval_ticks=360)
    assert day1["interventions"]["total"] == 1          # dm2 — 다음 날 경계가 없어 열린 창
    assert day1["responses"]["responded_interventions"] == 0

    # tick이 없는 날은 벽시계 창을 잡을 수 없다 — 개입 0의 우아한 영값
    day5 = await loop_health(conn, WORLD, day=5, interval_ticks=360)
    assert day5["interventions"]["total"] == 0
