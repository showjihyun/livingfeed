"""Personal/Private 피드 — Redis 타임라인 읽기 (ADR-014 fan-out-on-write).

쓰기는 redis-projector(lf_projector.timeline)가 소유한다 — 여기는 같은 키를
읽기만 한다. World/Community(fan-out-on-read, OpenSearch)와 달리 이 등급들은
플레이어 단위로 이미 편집된 목록이라 질의가 아니라 조회다.

등급 매핑: 타임라인 엔트리 중 visibility=private(1:1 답장·댓글)만 private이고,
나머지(아는 액터의 포스트)는 전부 personal이다 — "아는 사람의 소식이 내 피드다".
"""

from __future__ import annotations

import json
from typing import Any

from lf_projector.timeline import TimelineStore, ulid_ms

#: /feed의 types 중 타임라인이 서빙하는 등급 — OS 등급과 섞어 질의할 수 없다
TIMELINE_KINDS = frozenset({"personal", "private"})

#: 타임라인 원시 조회 상한 — ZSET은 쓰기 시 TIMELINE_CAP(500)로 잘리지만, 읽기에도
#: 명시 상한을 둬 캡 변경·이상 상태에서도 조회가 무제한 fetch가 되지 않게 한다 (방어).
#: kind/from_tick 필터가 조회 후라 페이지 크기보다 넉넉히 당겨야 하므로 캡과 같이 둔다.
TIMELINE_MAX_SCAN = 500


def entry_kind(doc: dict[str, Any]) -> str:
    return "private" if doc.get("visibility") == "private" else "personal"


async def read_timeline(
    redis: Any, world_id: str, player_id: str, kinds: list[str], *, limit: int,
    cursor: str | None, from_tick: int | None = None,
) -> dict[str, Any]:
    """타임라인 페이지 — /feed recent와 같은 응답 계약({items, next_cursor, mode}).

    from_tick: 조회 범위(오늘/이번 주/이번 달, ADR-011 세계일 좌표)의 tick 하한 —
    엔트리의 tick(모든 프로젝터 doc이 싣는다)으로 거른다. tick이 없는 엔트리는
    창에 놓을 수 없으니 범위 조회에서 제외한다 (정직한 침묵).
    """
    key = TimelineStore.timeline_key(world_id, player_id)
    if cursor is not None:
        # 커서(ULID)의 ms를 score 상한으로 — 이미 본 최신분을 서버에서 건너뛴다(키셋).
        # 상한은 포함(같은 ms 엔트리 보존)이고, 동점 ms 안의 순서는 아래 event_id
        # 비교가 마무리한다 — 필터·정렬 계약은 그대로다.
        raw = await redis.zrevrangebyscore(
            key, ulid_ms(cursor), "-inf", start=0, num=TIMELINE_MAX_SCAN
        )
    else:
        raw = await redis.zrevrange(key, 0, TIMELINE_MAX_SCAN - 1)
    docs = sorted(
        (json.loads(entry) for entry in raw),
        key=lambda doc: doc["event_id"],
        reverse=True,  # 같은 ms score 안에서도 event_id 내림차순을 보장한다
    )
    items = [
        doc for doc in docs
        if entry_kind(doc) in kinds
        and (cursor is None or doc["event_id"] < cursor)
        and (from_tick is None or doc.get("tick", -1) >= from_tick)
    ][:limit]
    return {
        "items": items,
        "next_cursor": items[-1]["event_id"] if items else None,
        "mode": "recent",
    }
