"""OpenSearch 피드 인덱스 — 매핑 정의와 최소 REST 클라이언트 (ADR-014 §2단).

프로젝션은 소모품이다 (ADR-003 계약 3): 인덱스는 언제든 파괴 후
이벤트 로그에서 재구축 가능해야 하므로 스키마 마이그레이션 도구를 두지 않는다.
매핑 변경 = drop() 후 --rebuild.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

DEFAULT_INDEX = "lf-feed-posts"

#: 봉투 메타 + payload 평탄화 문서의 명시적 매핑 — dynamic mapping 금지
#: (text/keyword 구분과 date 파싱이 랭킹 질의의 전제다, ADR-014)
MAPPING: dict[str, Any] = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "event_id": {"type": "keyword"},
            "world_id": {"type": "keyword"},
            "actor_id": {"type": "keyword"},
            "tick": {"type": "long"},
            "occurred_at": {"type": "date"},
            "causation_id": {"type": "keyword"},
            "correlation_id": {"type": "keyword"},
            "visibility": {"type": "keyword"},
            "title": {"type": "text"},
            "body": {"type": "text"},
            "narration_kind": {"type": "keyword"},
            "participants": {"type": "keyword"},
            "community_id": {"type": "keyword"},
            "location_id": {"type": "keyword"},
            "drama_score": {"type": "float"},
            "worthiness": {"type": "float"},
            "source_event_type": {"type": "keyword"},
            "tags": {"type": "keyword"},
            # 데뷔 포스트의 창조자 — '당신이 빚은 인물' 표식 원천 (페르소나 스튜디오)
            "created_by": {"type": "keyword"},
            "media": {"type": "object", "enabled": False},
        },
    },
}


def envelope_to_doc(envelope: dict[str, Any]) -> dict[str, Any]:
    """feed.post.published 봉투 → 색인 문서. _id는 event_id(=post id, 멱등 upsert)."""
    p = envelope["payload"]
    return {
        "event_id": envelope["event_id"],
        "world_id": envelope["world_id"],
        "actor_id": envelope["actor_id"],
        "tick": envelope["tick"],
        "occurred_at": envelope["occurred_at"],
        "causation_id": envelope["causation_id"],
        "correlation_id": envelope["correlation_id"],
        "visibility": p["visibility"],
        "title": p["title"],
        "body": p["body"],
        "narration_kind": p["narration_kind"],
        "participants": p["participants"],
        "community_id": p["community_id"],
        "location_id": p["location_id"],
        "drama_score": p["drama_score"],
        "worthiness": p["worthiness"],
        "source_event_type": p["source_event_type"],
        "tags": p["tags"],
        # 데뷔 포스트의 창조자 (페르소나 스튜디오) — '당신이 빚은 인물' 표식 원천
        "created_by": p.get("created_by"),
        "media": p["media"],
    }


def retire_query(world_id: str, actor_id: str) -> dict[str, Any]:
    """은퇴 소멸 질의 — actor_id(작성자) 일치 문서만 겨눈다 (actor.identity.retired).

    participants에만 낀 남의 포스트는 남는다 — 남의 글은 남의 역사다.
    """
    return {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"world_id": world_id}},
                    {"term": {"actor_id": actor_id}},
                ]
            }
        }
    }


def bulk_body(index: str, docs: list[dict[str, Any]]) -> str:
    """_bulk NDJSON — index 액션은 같은 _id를 덮어쓴다 (자연 멱등, ADR-003 계약 1)."""
    lines: list[str] = []
    for doc in docs:
        lines.append(json.dumps({"index": {"_index": index, "_id": doc["event_id"]}}))
        lines.append(json.dumps(doc, ensure_ascii=False))
    return "\n".join(lines) + "\n"


class OpenSearchIndex:
    """단일 인덱스에 대한 최소 비동기 클라이언트 (httpx 직결 — 무거운 SDK 회피)."""

    def __init__(self, base_url: str, index: str = DEFAULT_INDEX) -> None:
        self._index = index
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=10.0)

    @property
    def index(self) -> str:
        return self._index

    async def ensure(self) -> None:
        """인덱스가 없으면 매핑과 함께 생성한다 (멱등)."""
        head = await self._client.head(f"/{self._index}")
        if head.status_code == 404:
            r = await self._client.put(f"/{self._index}", json=MAPPING)
            r.raise_for_status()

    async def bulk_upsert(self, docs: list[dict[str, Any]]) -> None:
        if not docs:
            return
        r = await self._client.post(
            "/_bulk",
            content=bulk_body(self._index, docs).encode(),
            headers={"Content-Type": "application/x-ndjson"},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            failed = [
                item["index"]
                for item in body["items"]
                if item.get("index", {}).get("error")
            ]
            raise RuntimeError(f"bulk 색인 실패 {len(failed)}건: {failed[:3]}")

    async def delete_by_actor(self, world_id: str, actor_id: str) -> int:
        """은퇴 액터의 포스트 문서 소멸 — _delete_by_query (자연 멱등: 재실행은 0건).

        선행 refresh가 방금 색인된(아직 미가시) 문서까지 소멸 범위에 넣는다 —
        delete_by_query는 검색 스냅샷 기반이라 refresh 없이는 그것들을 놓친다.
        은퇴는 드문 사건이라 refresh 비용은 수용한다. 반환: 지운 문서 수.
        """
        (await self._client.post(f"/{self._index}/_refresh")).raise_for_status()
        r = await self._client.post(
            f"/{self._index}/_delete_by_query",
            params={"conflicts": "proceed", "refresh": "true"},
            json=retire_query(world_id, actor_id),
        )
        r.raise_for_status()
        return int(r.json().get("deleted", 0))

    async def drop(self) -> None:
        """재구축용 파괴 (ADR-003 계약 3). 없는 인덱스는 무시한다."""
        r = await self._client.delete(f"/{self._index}")
        if r.status_code not in (200, 404):
            r.raise_for_status()

    async def refresh(self) -> None:
        """색인 즉시 가시화 — 테스트/스모크 전용 (운영 경로에서 호출 금지)."""
        (await self._client.post(f"/{self._index}/_refresh")).raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()
