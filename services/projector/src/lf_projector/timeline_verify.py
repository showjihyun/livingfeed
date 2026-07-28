"""redis 팔로워 인덱스 무결성 검사 — 원천(es) 대비 (ADR-014 후속).

기대 집합은 두 원천의 fold다 (timeline.py의 쓰기 규칙과 동형):
관계 유래(액터↔플레이어 관계 스트림 키) ∪ 명시 팔로우 − 명시 철회(이긴다)
− 은퇴 액터(마지막 라이프사이클이 retired — retire_actor가 인덱스 키를 걷는다.
마지막이 returned면 부활 재사영이 되살렸으니 기대에 다시 포함된다).
타임라인(lf:tl) 자체는 상한·최종 일관성 때문에 개수 비교가 무의미해 검사하지
않는다 — 인덱스가 맞으면 다음 포스트부터의 팬아웃이 맞는다.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg import AsyncConnection
from redis.asyncio import Redis

from lf_projector.timeline import TimelineStore

logger = logging.getLogger("lf.projector.timeline_verify")

_REL_KEYS_SQL = (
    "SELECT DISTINCT stream_key FROM es.events"
    " WHERE world_id = %s AND stream = 'relationship'"
)
#: 접는 순서는 **프로젝터가 실제로 적용한 순서**여야 한다 — global_seq가 그 순서다
#: (replay.py도 global_seq 오름차순으로 먹인다). event_id(ULID)로 정렬하면 안 된다:
#: new_ulid는 같은 밀리초 안에서 80비트가 순수 난수라 단조가 아니고, 연달아 적재된
#: 선언/철회의 앞뒤가 뒤집혀 "마지막이 이긴다"가 무작위로 갈린다.
_FOLLOW_SQL = (
    "SELECT payload FROM es.events"
    " WHERE world_id = %s AND type = 'player.follow.changed' ORDER BY global_seq"
)
#: 액터별 마지막 라이프사이클(은퇴/부활) — 마지막이 retired인 액터만 은퇴자다
#: (부활이 이긴다 — reproject_returned가 인덱스·발신분을 되살렸다)
_LIFECYCLE_SQL = (
    "SELECT DISTINCT ON (actor_id) actor_id, type FROM es.events"
    " WHERE world_id = %s AND type IN"
    " ('actor.identity.retired', 'actor.identity.returned')"
    " ORDER BY actor_id, global_seq DESC"  # 같은 이유 — 은퇴/부활의 마지막을 고른다
)
_WORLDS_SQL = (
    "SELECT DISTINCT world_id FROM es.events"
    " WHERE stream = 'relationship' OR type = 'player.follow.changed'"
)


def fold_followers(
    rel_keys: list[str],
    follow_payloads: list[dict[str, Any]],
    retired: frozenset[str] | set[str] = frozenset(),
) -> dict[str, set[str]]:
    """원천 → 기대 팔로워 (순수 fold — timeline.py 쓰기 규칙과 동형).

    관계 키("from|to")의 액터↔플레이어 쌍이 stand-in으로 들어가고, 명시
    선언은 (player, actor)당 마지막이 이긴다 — 철회면 stand-in도 밀어낸다.
    은퇴 액터(retired 집합 — 마지막 라이프사이클이 retired)는 통째로 빠진다
    (retire_actor가 인덱스 키를 걷는다 — 은퇴는 관계·선언보다 항상 나중이라는
    전제, 은퇴와 부활 사이의 이벤트는 세계에 없다).
    """
    followers: dict[str, set[str]] = {}
    for key in rel_keys:
        from_id, _, to_id = key.partition("|")
        if not to_id:
            continue
        from_player, to_player = from_id.startswith("p_"), to_id.startswith("p_")
        if from_player == to_player:  # 액터↔액터는 팔로우가 아니다 (follower_pair와 동형)
            continue
        actor, player = (to_id, from_id) if from_player else (from_id, to_id)
        followers.setdefault(actor, set()).add(player)

    last: dict[tuple[str, str], bool] = {}
    for payload in follow_payloads:  # event_id 순 — 마지막 선언이 이긴다
        last[(payload["player_id"], payload["target_actor_id"])] = bool(payload["following"])
    for (player, actor), following in last.items():
        bucket = followers.setdefault(actor, set())
        if following:
            bucket.add(player)
        else:
            bucket.discard(player)
    return {
        actor: players
        for actor, players in followers.items()
        if players and actor not in retired
    }


async def verify_timeline_world(
    conn: AsyncConnection, redis: Redis, world_id: str
) -> dict[str, Any]:
    """세계 하나의 팔로워 인덱스 무결성 — 액터별 기대/실측 집합 비교."""
    rel_keys = [
        r[0] for r in await (await conn.execute(_REL_KEYS_SQL, (world_id,))).fetchall()
    ]
    follows = [
        r[0] for r in await (await conn.execute(_FOLLOW_SQL, (world_id,))).fetchall()
    ]
    retired = {
        actor
        for actor, type_ in await (await conn.execute(_LIFECYCLE_SQL, (world_id,))).fetchall()
        if type_ == "actor.identity.retired"
    }
    expected = fold_followers(rel_keys, follows, retired)

    store = TimelineStore(redis)
    actual: dict[str, set[str]] = {}
    prefix = f"lf:tlflw:{world_id}:"
    async for key in redis.scan_iter(match=f"{prefix}*"):
        name = key.decode() if isinstance(key, bytes) else key
        actor = name.removeprefix(prefix)
        members = await store.followers(world_id, actor)
        if members:
            actual[actor] = members

    mismatched = {
        actor: {
            "expected": sorted(expected.get(actor, set())),
            "actual": sorted(actual.get(actor, set())),
        }
        for actor in set(expected) | set(actual)
        if expected.get(actor, set()) != actual.get(actor, set())
    }
    report = {
        "ok": not mismatched,
        "actors": len(set(expected) | set(actual)),
        "mismatched": mismatched,
    }
    logger.info(
        "팔로워 인덱스 무결성 %s — world=%s actors=%d mismatched=%d",
        "OK" if report["ok"] else "MISMATCH", world_id,
        report["actors"], len(mismatched),
    )
    return report


async def _index_worlds(redis: Redis) -> set[str]:
    """인덱스 쪽 세계 열거 — lf:tlflw:{world}:{actor} 키에서 파싱한다."""
    worlds: set[str] = set()
    async for key in redis.scan_iter(match="lf:tlflw:*"):
        name = key.decode() if isinstance(key, bytes) else key
        world, _, actor = name.removeprefix("lf:tlflw:").partition(":")
        if actor:
            worlds.add(world)
    return worlds


async def verify_timeline(
    conn: AsyncConnection, redis: Redis, *, world_id: str | None = None
) -> dict[str, Any]:
    """세계별 팔로워 인덱스 무결성 — world_id를 주면 그 세계만, 아니면 원천∪인덱스.

    인덱스 쪽 세계도 합쳐야 고아 프로젝션(원천에 없는 세계의 팔로워 키)이
    보인다 — 원천만 보면 그런 세계는 검사 자체가 건너뛰어진다.
    """
    if world_id is not None:
        worlds = [world_id]
    else:
        rows = await (await conn.execute(_WORLDS_SQL)).fetchall()
        worlds = sorted({w for (w,) in rows} | await _index_worlds(redis))
    reports = {world: await verify_timeline_world(conn, redis, world) for world in worlds}
    return {"ok": all(r["ok"] for r in reports.values()), "worlds": reports}
