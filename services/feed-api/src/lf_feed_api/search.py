"""OpenSearch 피드 질의 — 랭킹 첫 화면 + 커서 이어보기 (ADR-014 §2단).

두 읽기 모드:
- ranked: function_score 랭킹 + correlation_id collapse(다양성 보정).
  첫 화면은 편집된 랭킹이다. 시간 순서가 아니므로 커서를 지원하지 않는다.
- recent: event_id(ULID) 내림차순 + search_after — 결정적 커서 페이지네이션.
  커서는 post id(ULID)로, SSE의 Last-Event-ID와 같은 좌표계다 (ADR-010).
"""

from __future__ import annotations

from typing import Any

import httpx

from lf_feed_api.config import Config


def visibility_filter(world_id: str, kinds: list[str]) -> list[dict[str, Any]]:
    return [
        {"term": {"world_id": world_id}},
        {"terms": {"visibility": kinds}},
    ]


def build_ranked_query(cfg: Config, world_id: str, kinds: list[str], limit: int) -> dict[str, Any]:
    """score = w_drama·drama + w_recency·gauss(occurred_at). collapse = 다양성 보정.

    관계 근접도 항(w_proximity)은 Relationship Engine(ADR-016)이 생기면 추가된다.
    """
    return {
        "size": limit,
        "query": {
            "function_score": {
                "query": {"bool": {"filter": visibility_filter(world_id, kinds)}},
                "functions": [
                    {
                        "field_value_factor": {
                            "field": "drama_score", "factor": cfg.w_drama, "missing": 0,
                        }
                    },
                    {
                        "gauss": {
                            "occurred_at": {"origin": "now", "scale": cfg.recency_scale}
                        },
                        "weight": cfg.w_recency,
                    },
                ],
                "score_mode": "sum",
                "boost_mode": "replace",
            }
        },
        # 같은 서사 사슬(correlation)의 도배 방지 — 피드가 세계를 왜곡하면 제품이 자멸한다
        "collapse": {"field": "correlation_id"},
        "sort": ["_score", {"event_id": "desc"}],
    }


def build_recent_query(
    world_id: str, kinds: list[str], limit: int, cursor: str | None
) -> dict[str, Any]:
    query: dict[str, Any] = {
        "size": limit,
        "query": {"bool": {"filter": visibility_filter(world_id, kinds)}},
        "sort": [{"event_id": "desc"}],
    }
    if cursor:
        query["search_after"] = [cursor]
    return query


class FeedSearch:
    """읽기 전용 질의 클라이언트 — 프로젝션만 읽는다 (ADR-003 읽기 API 규칙)."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.opensearch_url.rstrip("/"), timeout=5.0
        )

    async def search(
        self, world_id: str, kinds: list[str], *, limit: int, sort: str, cursor: str | None
    ) -> dict[str, Any]:
        cfg = self._cfg
        body = (
            build_recent_query(world_id, kinds, limit, cursor)
            if sort == "recent"
            else build_ranked_query(cfg, world_id, kinds, limit)
        )
        r = await self._client.post(f"/{cfg.index}/_search", json=body)
        r.raise_for_status()
        hits = r.json()["hits"]["hits"]
        items = [h["_source"] for h in hits]
        next_cursor = items[-1]["event_id"] if items and sort == "recent" else None
        return {"items": items, "next_cursor": next_cursor, "mode": sort}

    async def close(self) -> None:
        await self._client.aclose()
