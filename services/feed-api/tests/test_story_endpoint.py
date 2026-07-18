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


def test_summarize_unknown_type_is_generic_sentence_not_type_label():
    # 타입 라벨 폴백은 기계 어휘 유출이었다 — 항목은 남되(침묵 실패 회피,
    # 구조 필드 type이 관찰 좌표) 문장은 일반 문장으로 접는다
    line = summarize("actor.confession.made", {"depth": 1})
    assert line == "무언가가 일어났다"
    assert "actor.confession.made" not in line


# ── 실사고 재현 — 엔진 사유(reason/note)의 기계 형식 유출 (2026-07 실화면) ──


def test_emotion_machine_reason_is_folded_to_interaction_sentence():
    # 실화면 유출 원문 그대로: 타입 토큰·감정 코드·수치·플레이어 id
    leak = "player.comment.posted — gratitude 0.75 (플레이어 p_observer_0417)"
    line = summarize("actor.emotion.shifted", {"reason": leak})
    assert line == "댓글에 마음이 움직였다"
    for token in ("player.comment.posted", "gratitude", "0.75", "p_observer_0417"):
        assert token not in line


def _emotion(reason: str | None) -> str:
    payload = {} if reason is None else {"reason": reason}
    return summarize("actor.emotion.shifted", payload)


def test_emotion_reason_variants_never_leak_machine_vocab():
    # 좋아요·DM·공명(feed.post)·목표(goal.*) — emotion 엔진의 reason 형식 전 계열
    assert _emotion("player.reaction.added — joy 0.25 (플레이어 p_x)") == "좋아요에 마음이 움직였다"
    assert _emotion("player.dm.sent — gratitude 0.55 (플레이어 p_x)") == "DM에 마음이 움직였다"
    assert _emotion("feed.post — envy 0.42 (a_hana의 글)") == "글에 마음이 움직였다"
    # 모르는 토큰·수치만 있는 사유 — 일반 문장으로 (마디는 남긴다)
    assert _emotion("goal.advanced — joy 0.30") == "마음이 움직였다"
    assert _emotion(None) == "마음이 움직였다"


def _relationship(reason: str) -> str:
    return summarize("relationship.state.changed", {"reason": reason})


def test_relationship_machine_reason_is_folded():
    # relationship 엔진 형식: "{source_kind} ({direction})" / "감정 응고: {type} {수치}"
    assert _relationship("player.comment.posted (received)") == "댓글에 마음의 거리가 달라졌다"
    assert _relationship("감정 응고: gratitude 0.69") == "마음의 거리가 달라졌다"
    # 사람 문장 사유는 그대로 살린다 (샘플·인물 통찰 계열)
    assert _relationship("인물 통찰 — 비중이 는다") == "인물 통찰 — 비중이 는다"


def test_milestone_dirty_note_falls_back_to_kind_label():
    assert summarize(
        "relationship.milestone.reached",
        {"milestone": "betrayal", "note": "stage acquaintance 0.4"},
    ) == "믿음이 배신으로 무너졌다"
    assert summarize(
        "relationship.milestone.reached", {"milestone": "who_knows"}
    ) == "관계가 새로운 국면으로 넘어갔다"


def test_director_numeric_reason_is_folded_to_stage_sentence():
    # 규칙 개입 reason은 수치 보고서다 — LLM rationale(사람 문장)만 살린다
    leak = "침체 감지 — drama 이동평균 0.12이 5 tick 지속 (임계 0.3/4)"
    folded = summarize("system.director.intervened", {"reason": leak})
    assert folded == "세계가 조용히 무대를 움직였다"
    human = "아리의 특종이 벽에 부딪히게 한다"
    assert summarize("system.director.intervened", {"reason": human}) == human


def test_skeleton_items_survive_empty_payload_with_human_fallback():
    # 뼈대(시작점·포스트·응답)는 문장이 비어도 접히지 않는다
    assert summarize("player.comment.posted", {}) == "댓글을 남겼다"
    assert summarize("player.dm.sent", {}) == "DM을 보냈다"
    assert summarize("feed.post.published", {}) == "이야기를 실었다"
    assert summarize("actor.message.sent", {}) == "말을 건넸다"


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
