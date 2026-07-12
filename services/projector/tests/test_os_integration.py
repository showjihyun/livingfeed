"""OpenSearch 실색인 통합 — LF_TEST_OPENSEARCH_URL 있을 때만 (없으면 skip).

로컬: docker compose --profile core up -d 후
      LF_TEST_OPENSEARCH_URL=http://localhost:9200 uv run pytest services/projector
CI에는 OpenSearch 컨테이너가 없어 스킵된다 — E2E 스모크로 보완 (로드맵 7단계).
"""

import json
import os
from pathlib import Path

import httpx
import pytest
from lf_projector.os_index import OpenSearchIndex, envelope_to_doc

OS_URL = os.environ.get("LF_TEST_OPENSEARCH_URL")

pytestmark = pytest.mark.skipif(
    OS_URL is None, reason="LF_TEST_OPENSEARCH_URL 미설정 — OpenSearch 통합 스킵"
)

SAMPLE = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages" / "schemas" / "samples" / "feed.post.published.001.json"
    ).read_text(encoding="utf-8")
)


async def test_ensure_upsert_search_drop_roundtrip():
    index = OpenSearchIndex(OS_URL, "lf-feed-posts-test")
    try:
        await index.drop()  # 이전 실행 잔재 제거
        await index.ensure()
        await index.ensure()  # 멱등

        doc = envelope_to_doc(SAMPLE)
        await index.bulk_upsert([doc])
        await index.bulk_upsert([doc])  # 같은 _id 덮어쓰기 — 중복 문서 없어야 한다
        await index.refresh()

        async with httpx.AsyncClient(base_url=OS_URL.rstrip("/")) as client:
            r = await client.post(
                "/lf-feed-posts-test/_search",
                json={"query": {"term": {"visibility": "world"}}},
            )
            r.raise_for_status()
            hits = r.json()["hits"]
            assert hits["total"]["value"] == 1
            assert hits["hits"][0]["_id"] == SAMPLE["event_id"]
    finally:
        await index.drop()
        await index.close()
