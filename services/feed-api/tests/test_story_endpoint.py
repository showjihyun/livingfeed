"""GET /story/{correlation_id} 계약 + 사슬 서사 규칙 (plan/03 §단계 3→4 저자성).

계약 검증은 대역 주입(기존 /feed 테스트 방식), es/read 실왕복은
LF_TEST_* 게이트가 있는 통합 테스트(test_story_integration.py)가 맡는다.
"""

from fastapi.testclient import TestClient
from lf_feed_api.config import Config
from lf_feed_api.main import create_app
from lf_feed_api.story import display_actor, is_narrative, summarize

CORR = "01JZK7Q3W0000000000000000B"
PLAYER = "p_observer_0417"


# ── 무서사 제외 — 블랙리스트 (근거는 story.py 주석) ──────────────────────


def test_tick_heartbeat_is_not_narrative():
    assert not is_narrative("system.tick.started")
    assert not is_narrative("system.tick.completed")


def test_director_stage_knobs_are_not_narrative():
    for type_ in (
        "system.director.feed_boosted",
        "system.director.spotlighted",
        "system.director.season_set",
    ):
        assert not is_narrative(type_)


def test_story_events_are_narrative():
    for type_ in (
        "player.dm.sent", "actor.action.performed", "feed.post.published",
        "system.director.intervened", "world.incident.occurred",
    ):
        assert is_narrative(type_)


def test_unknown_type_is_narrative_by_default():
    # 새 타입은 기본 포함 — 이야기를 빠뜨리는 쪽보다 낯선 항목이 섞이는 쪽이 낫다
    assert is_narrative("actor.confession.made")


# ── 타입별 한글 한 줄 요약 ────────────────────────────────────────────


def test_summarize_extracts_human_line_per_type():
    assert summarize("actor.action.performed", {"intent": "말을 건다"}) == "말을 건다"
    assert summarize("feed.post.published", {"title": "김아리, 말을 걸다"}) == "김아리, 말을 걸다"
    assert summarize("player.dm.sent", {"text": "힘내요"}) == "힘내요"
    assert summarize("actor.emotion.shifted", {"reason": "인정받았다"}) == "인정받았다"
    assert summarize("world.incident.occurred", {"description": "카페의 정전"}) == "카페의 정전"
    assert summarize("relationship.milestone.reached", {"note": "처음 연결됐다"}) == "처음 연결됐다"


def test_summarize_reaction_and_follow_are_human_sentences():
    like = summarize("player.reaction.added", {"kind": "like"})
    assert "좋아요" in like and "like" not in like  # 내부 표기가 아니라 사람 문장
    follow = summarize("player.follow.changed", {"following": True})
    unfollow = summarize("player.follow.changed", {"following": False})
    assert follow != unfollow


def test_summarize_unknown_type_falls_back_to_type_label():
    assert summarize("actor.confession.made", {"depth": 1}) == "actor.confession.made"


# ── 표시 이름 해석 — read.actors + '당신' 치환 ───────────────────────


def test_actor_names_resolve_from_read_actors():
    names = {"a_aria_kim": "김아리"}
    assert display_actor("a_aria_kim", {}, names=names, requester=PLAYER) == "김아리"
    # 이름 미상 액터는 '누군가' — 식별자를 화면 문장에 내보내지 않는다 (FE 내레이터 규약)
    assert display_actor("a_ghost", {}, names=names, requester=PLAYER) == "누군가"


def test_requester_player_is_you_and_others_stay_anonymous():
    mine = {"player_id": PLAYER}
    other = {"player_id": "p_stranger"}
    assert display_actor(None, mine, names={}, requester=PLAYER) == "당신"
    assert display_actor(None, other, names={}, requester=PLAYER) == "어느 관찰자"
    # 요청자 미상이면 아무도 '당신'이 아니다
    assert display_actor(None, mine, names={}, requester=None) == "어느 관찰자"


def test_actorless_world_event_speaks_as_world():
    assert display_actor(None, {"description": "정전"}, names={}, requester=PLAYER) == "세계"


# ── 엔드포인트 계약 (대역 주입 — lifespan 미실행이라 실 연결 없음) ───────


class FakeStory:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def timeline(self, world_id, correlation_id, *, player_id, limit):
        self.calls.append((world_id, correlation_id, player_id, limit))
        return {
            "world_id": world_id, "correlation_id": correlation_id,
            "items": [], "origin": None, "started_by_you": False,
        }


def make_client() -> tuple[TestClient, FakeStory]:
    story = FakeStory()
    cfg = Config(opensearch_url="http://unused", redis_url="redis://unused")
    # search/cache/reads는 /story가 닿지 않는다 — 주입만 해서 lifespan 소유를 막는다
    app = create_app(cfg=cfg, search=object(), cache=object(), reads=object(), story=story)
    return TestClient(app), story


def test_story_delegates_with_default_limit_knob():
    client, story = make_client()
    resp = client.get(f"/story/{CORR}", params={"player_id": PLAYER})
    assert resp.status_code == 200
    assert story.calls == [("w_main", CORR, PLAYER, 50)]
    body = resp.json()
    assert body["correlation_id"] == CORR
    assert body["started_by_you"] is False


def test_story_player_id_is_optional():
    client, story = make_client()
    assert client.get(f"/story/{CORR}").status_code == 200
    assert story.calls == [("w_main", CORR, None, 50)]


def test_story_limit_clamped_to_max():
    client, story = make_client()
    assert client.get(f"/story/{CORR}", params={"limit": 500}).status_code == 200
    assert story.calls[-1][3] == 100


def test_story_rejects_bad_ids():
    client, _ = make_client()
    assert client.get("/story/not-a-ulid").status_code == 422  # 경로 패턴(ULID)
    assert client.get(f"/story/{CORR}", params={"player_id": "nope"}).status_code == 422


def test_story_unavailable_returns_503_without_killing_feed():
    cfg = Config(opensearch_url="http://unused", redis_url="redis://unused")
    app = create_app(cfg=cfg, search=object(), cache=object(), reads=object())
    app.state.story = None  # lifespan이 PG 실패로 내려놓은 상태를 재현
    client = TestClient(app)
    assert client.get(f"/story/{CORR}").status_code == 503
