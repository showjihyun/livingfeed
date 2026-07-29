"""Semantic 기억 — 응고된 기억의 임베딩 저장·회상 (ADR-007/008 §Semantic).

임베딩: 결정적 해시 n-gram 벡터 (LLM 불요 — dev/CI에서도 실제 유사도 검색이
동작하고 리플레이가 재현된다). 모델 임베딩(ai-runtime 'embed' task)은
프로바이더가 붙는 단계의 후속이며, 이 모듈의 인터페이스는 그대로 간다.

망각은 기능이다 (ADR-008 규칙 3): 포인트는 decay_at을 갖고, 회상은
만료 전 것만 본다. 원본(Episodic 이벤트)은 지워지지 않는다.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
import uuid
from dataclasses import dataclass

import httpx

logger = logging.getLogger("lf.actor.semantic")

EMBED_DIM = 256


@dataclass(frozen=True)
class Recollection:
    """회상된 기억 한 조각 — 본문과 그 출처.

    본문만 돌려주면 프롬프트는 만들 수 있어도 "어떤 기억이 이 결정에
    들어갔나"를 사후에 답할 수 없다 (ADR-021 §2 sections[].source_ids).
    Qdrant payload에 event_id가 이미 실려 있어 추가 조회 없이 따라온다.
    """

    event_id: str
    text: str

#: 망각 곡선 — 중요도 0이면 1일, 1이면 30일 (실시간 기준, Phase 1 단순화)
DECAY_MIN_S = 86_400
DECAY_MAX_S = 30 * 86_400

#: 회상 기본 하한 — 응고 게이트(0.4)보다 의도적으로 낮다 (ADR-008 §접근 인터페이스)
RECALL_MIN_IMPORTANCE = 0.3


def embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """결정적 해시 n-gram 임베딩 — 같은 텍스트는 같은 벡터 (리플레이 재현).

    문자 2/3-gram을 해시 버킷에 누적하고 L2 정규화한다. 표현력은 모델
    임베딩보다 낮지만 어휘 겹침 기반 회상에는 충분하다 (Phase 1).
    """
    counts = [0.0] * dim
    text = text.strip().lower()
    for n in (2, 3):
        for i in range(max(0, len(text) - n + 1)):
            gram = text[i : i + n]
            digest = hashlib.blake2s(gram.encode(), digest_size=4).digest()
            bucket = int.from_bytes(digest, "big")
            counts[bucket % dim] += 1.0
    norm = math.sqrt(sum(c * c for c in counts))
    if norm == 0:
        return counts
    return [round(c / norm, 6) for c in counts]


def _point_id(event_id: str) -> str:
    """ULID → UUID (Qdrant 포인트 id 요구 형식) — 결정적 매핑."""
    return str(uuid.UUID(bytes=hashlib.blake2s(event_id.encode(), digest_size=16).digest()))


class SemanticMemory:
    """Qdrant 컬렉션(세계당 1개)의 최소 REST 클라이언트 — 실패는 우회 (기억은 최적화)."""

    def __init__(self, base_url: str, *, dim: int = EMBED_DIM) -> None:
        self._dim = dim
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=5.0)
        self._ensured: set[str] = set()

    @staticmethod
    def _collection(world_id: str) -> str:
        return f"lf_semantic_{world_id}"

    async def _ensure(self, world_id: str) -> None:
        name = self._collection(world_id)
        if name in self._ensured:
            return
        r = await self._client.put(
            f"/collections/{name}",
            json={"vectors": {"size": self._dim, "distance": "Cosine"}},
        )
        # 409(이미 존재)는 멱등 성공이다
        if r.status_code not in (200, 409):
            r.raise_for_status()
        self._ensured.add(name)

    async def remember(
        self,
        world_id: str,
        actor_id: str,
        *,
        event_id: str,
        text: str,
        importance: float,
        source_event_ids: list[str],
        now_s: float | None = None,
        point_key: str | None = None,
    ) -> bool:
        """응고 기억을 임베딩 저장한다. 실패는 False — 응고 이벤트(원본)는 이미 안전하다.

        point_key를 주면 그 키로 포인트 id를 파생한다 — 신념처럼 같은 자리의
        기억이 갱신(덮어쓰기)되는 경우에 쓴다 (reflection, ADR-008).
        """
        now_s = now_s if now_s is not None else time.time()
        decay_at = now_s + DECAY_MIN_S + (DECAY_MAX_S - DECAY_MIN_S) * importance
        try:
            await self._ensure(world_id)
            r = await self._client.put(
                # wait=true — 색인이 끝난 뒤 응답한다. 기본(false)은 접수만 확인하므로
                # remember()가 True를 돌려준 직후의 회상이 그 기억을 못 볼 수 있다:
                # "저장됐다"가 "회상 가능하다"를 뜻하지 않는 반쪽 계약이 된다.
                # 응고는 tick당 한 번이라 대기 비용은 무해하고, 갓 기동한 서버에서
                # 드러나는 경합(CI에서 실제로 실패했다)이 여기서 닫힌다.
                f"/collections/{self._collection(world_id)}/points?wait=true",
                json={
                    "points": [
                        {
                            "id": _point_id(point_key or event_id),
                            "vector": embed(text, self._dim),
                            "payload": {
                                "actor_id": actor_id,
                                "text": text,
                                "importance": round(importance, 4),
                                "source_event_ids": source_event_ids,
                                "event_id": event_id,
                                "decay_at": decay_at,
                            },
                        }
                    ]
                },
            )
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning("Semantic 저장 실패(우회 — 원본은 Episodic에 있다): %s", e)
            return False

    async def recall(
        self,
        world_id: str,
        actor_id: str,
        query: str,
        *,
        k: int = 3,
        min_importance: float = RECALL_MIN_IMPORTANCE,
        now_s: float | None = None,
    ) -> list[Recollection]:
        """자기 기억만, 만료 전 것만, 유사한 것부터 (ADR-008 규칙 3/5)."""
        now_s = now_s if now_s is not None else time.time()
        try:
            await self._ensure(world_id)
            r = await self._client.post(
                f"/collections/{self._collection(world_id)}/points/search",
                json={
                    "vector": embed(query, self._dim),
                    "limit": k,
                    "with_payload": True,
                    "filter": {
                        "must": [
                            {"key": "actor_id", "match": {"value": actor_id}},
                            {"key": "importance", "range": {"gte": min_importance}},
                            {"key": "decay_at", "range": {"gt": now_s}},
                        ]
                    },
                },
            )
            r.raise_for_status()
            return [
                Recollection(
                    # 구 포인트엔 event_id가 없을 수 있다 — 본문은 살리고 출처만 빈다
                    event_id=hit["payload"].get("event_id", ""),
                    text=hit["payload"]["text"],
                )
                for hit in r.json()["result"]
            ]
        except Exception as e:
            logger.warning("회상 실패(빈 회상으로 우회): %s", e)
            return []

    async def close(self) -> None:
        await self._client.aclose()
