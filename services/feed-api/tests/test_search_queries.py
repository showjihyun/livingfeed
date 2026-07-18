"""OpenSearch 질의 생성 검증 — 랭킹 계수와 다양성 보정 (ADR-014 §2단)."""

from lf_feed_api.config import Config
from lf_feed_api.search import (
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
