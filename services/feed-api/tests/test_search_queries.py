"""OpenSearch 질의 생성 검증 — 랭킹 계수와 다양성 보정 (ADR-014 §2단)."""

from lf_feed_api.config import Config
from lf_feed_api.search import build_ranked_query, build_recent_query

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
