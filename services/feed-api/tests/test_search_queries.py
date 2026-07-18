"""OpenSearch 질의 생성 검증 — 랭킹 계수와 다양성 보정 (ADR-014 §2단).

아이템 매핑의 읽기 경계 정화(과거 발행분 원시 id)도 여기서 고정한다 —
es·OS 인덱스는 불변이라 composer 정화 이전 발행분은 읽기에서 걷어낸다.
"""

from typing import Any

from lf_feed_api.config import Config
from lf_feed_api.search import (
    FeedSearch,
    build_ranked_query,
    build_recent_query,
    build_text_search_query,
)

CFG = Config(opensearch_url="http://unused", redis_url="redis://unused")


def test_ranked_query_encodes_coefficients_and_collapse():
    q = build_ranked_query(CFG, "w_main", ["world"], 20)
    fs = q["query"]["function_score"]
    assert fs["boost_mode"] == "replace"  # BM25가 아니라 편집 점수가 랭킹이다
    drama_fn, recency_fn = fs["functions"]
    assert drama_fn["field_value_factor"]["factor"] == CFG.w_drama
    assert recency_fn["weight"] == CFG.w_recency
    # 같은 서사 사슬 도배 방지 — 다양성 보정은 하드 컷으로 집행 (ADR-014 반목표)
    assert q["collapse"] == {"field": "correlation_id"}
    assert {"term": {"world_id": "w_main"}} in fs["query"]["bool"]["filter"]


def test_recent_query_paginates_by_ulid():
    q = build_recent_query("w_main", ["world", "personal"], 20, cursor=None)
    assert q["sort"] == [{"event_id": "desc"}]
    assert "search_after" not in q

    q2 = build_recent_query("w_main", ["world"], 20, cursor="01JZK7Q3W0000000000000000A")
    assert q2["search_after"] == ["01JZK7Q3W0000000000000000A"]


def test_text_search_matches_title_weighted_and_body():
    q = build_text_search_query("w_main", ["world"], "오디션", [], 20)
    bool_q = q["query"]["bool"]
    assert {"term": {"world_id": "w_main"}} in bool_q["filter"]  # 가시성 필터는 불변
    [match] = bool_q["should"]
    assert match["multi_match"]["fields"] == ["title^2", "body"]  # 제목이 본문보다 무겁다
    assert bool_q["minimum_should_match"] == 1
    # 관련도순, 동점은 최신(ULID 내림차순) — 검색은 역사를 최신부터 보여준다
    assert q["sort"] == ["_score", {"event_id": "desc"}]


def test_text_search_includes_author_ids_when_resolved():
    """이름 역해석(FE 로스터)이 준 작성자 id는 본문 일치와 or로 결합된다."""
    q = build_text_search_query("w_main", ["world"], "김아리", ["a_aria_kim"], 20)
    should = q["query"]["bool"]["should"]
    assert {"terms": {"actor_id": ["a_aria_kim"]}} in should
    assert len(should) == 2  # 본문 multi_match + 작성자 terms


# ── 아이템 매핑의 읽기 경계 정화 — 과거 발행분의 원시 id (실사고 확장) ─────


class FakeResponse:
    def __init__(self, hits: list[dict]) -> None:
        self._hits = hits

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"hits": {"hits": self._hits}}


class FakeOSClient:
    def __init__(self, hits: list[dict]) -> None:
        self._hits = hits

    async def post(self, url: str, json: Any) -> FakeResponse:
        return FakeResponse(self._hits)


def make_search(hits: list[dict], names: dict[str, str] | None = None) -> FeedSearch:
    fs = FeedSearch(CFG, actor_names=names)
    fs._client = FakeOSClient(hits)  # OS 왕복 대역 — 매핑·정화만 검증
    return fs


#: 과거 발행분 그대로 — composer 정화(compose.humanize_ids) 이전의 인덱스 문서
STALE_SOURCE = {
    "event_id": "01JZK7Q3W0000000000000000F",
    "actor_id": "a_aria_kim",
    "title": "김아리, p_observer_0417에게 답하다",
    "body": "…p_observer_0417에게 전하고 싶었다. a_junho_park도 알 것이다",
    "correlation_id": "01JZK7Q3W0000000000000000B",
}


async def test_search_items_anonymize_player_ids_in_title_and_body():
    fs = make_search([{"_source": dict(STALE_SOURCE), "_score": 1.2}])
    for sort in ("ranked", "recent"):
        out = await fs.search("w_main", ["world"], limit=20, sort=sort, cursor=None)
        [item] = out["items"]
        assert item["title"] == "김아리, 어느 관찰자에게 답하다"
        assert "p_observer_0417" not in item["body"]
        # 이름 원천 미주입 계층 — 액터 id는 뭉개지 않는다 (그라운딩은 FE 로스터의 몫)
        assert "a_junho_park" in item["body"]
        # 구조 필드는 불변 — actor_id는 근접도 재랭킹, event_id는 커서의 좌표다
        assert item["actor_id"] == "a_aria_kim"
        assert item["event_id"] == STALE_SOURCE["event_id"]
    # ranked는 _score 동봉, recent는 커서가 정화와 무관하게 살아 있다
    assert out["next_cursor"] == STALE_SOURCE["event_id"]


async def test_text_search_items_are_sanitized_too():
    fs = make_search([{"_source": dict(STALE_SOURCE)}])
    out = await fs.text_search("w_main", ["world"], q="답하다", actor_ids=[], limit=20)
    [item] = out["items"]
    assert "p_observer_0417" not in item["title"]
    assert "어느 관찰자" in item["title"]
    assert item["actor_id"] == "a_aria_kim"


async def test_injected_names_ground_actor_ids_in_sentences():
    # 이름 주입은 옵션 계약 — 주입되면 액터 id도 실명(미상은 '누군가')으로
    fs = make_search(
        [{"_source": dict(STALE_SOURCE)}], names={"a_junho_park": "박준호"}
    )
    out = await fs.text_search("w_main", ["world"], q="답하다", actor_ids=[], limit=20)
    [item] = out["items"]
    assert "박준호도 알 것이다" in item["body"]
    assert "어느 관찰자에게" in item["body"]
