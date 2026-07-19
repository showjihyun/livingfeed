"""커뮤니티 레지스트리 — 표시 이름·소개의 원천 (ADR-014 Community 등급).

agents/communities.yaml 을 읽어 {id, name, description} 목록을 준다. 소속 판정은
페르소나 파일(community: c_*)이 SoT이고, 이 파일은 커뮤니티의 이름표일 뿐이다.
파일이 없거나 깨져도 목록만 빈 채로 강등한다 — 피드 경로를 멈추지 않는다.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("lf.feed_api.communities")

_COMMUNITY_ID = re.compile(r"^c_[a-z0-9_]+$")


def load_communities(path: str | Path) -> list[dict[str, str]]:
    """커뮤니티 목록 (파일 순서 보존). 미존재·손상은 빈 리스트로 강등."""
    p = Path(path)
    if not p.is_file():
        logger.info("커뮤니티 파일 없음: %s — 빈 목록", p)
        return []
    try:
        docs = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    except Exception:
        logger.warning("커뮤니티 파일 파싱 실패: %s — 빈 목록", p, exc_info=True)
        return []
    out: list[dict[str, str]] = []
    for doc in docs if isinstance(docs, list) else []:
        cid = doc.get("id") if isinstance(doc, dict) else None
        if not cid or not _COMMUNITY_ID.match(str(cid)):
            continue  # id 없는/형식 아닌 항목은 목록에 세울 수 없다
        out.append(
            {
                "id": str(cid),
                "name": str(doc.get("name") or cid),
                "description": str(doc.get("description") or ""),
            }
        )
    return out


def community_ids(communities: list[dict[str, Any]]) -> set[str]:
    return {c["id"] for c in communities}
