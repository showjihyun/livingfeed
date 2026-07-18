"""루프 헬스 계측 — 도파민 루프의 서버측 정직한 프록시 (docs/plan/02 §측정).

플레이어 개입(DM·댓글·좋아요·팔로우)이 반응으로 돌아오고(응답), 그 반응이
재개입과 2차 사건으로 이어졌는지를 봉투 열에서 접는다. '반응 확인'(클라이언트
열람)은 서버가 모른다 — 여기서 재는 것은 전부 이벤트 스토어가 보는 프록시다:

- 응답 발생률: 응답 의무가 있는 개입(DM·댓글) 중 actor.message.sent 응답이
  달린 비율 (causation 매칭 — 선제 DM·액터간 댓글은 응답이 아니다)
- 응답 지연: 개입 적재 → 첫 응답 적재 초의 p50/p95 (plan/02 예산은 '첫 반응
  토큰'이지만 우리가 보는 것은 이벤트 적재 시각이다 — 상한 프록시)
- 재개입률: 응답을 받은 플레이어 중 그 응답 이후 다시 개입한 비율
  (루프 재회전의 서버측 증거 — '확인'의 가장 정직한 대용)
- 드라마 재생산율: 개입 correlation을 승계한 feed.post.published / 개입 수

fold_loop_health는 순수 함수다: 같은 봉투 열 → 같은 리포트 (리플레이 재현).
loop_health는 season_retrospective와 동형의 시즌-일 창 조회다 — 단, 플레이어
개입은 tick 0 규약(gateway)이라 tick 창이 아니라 그 날 tick 스트림의 벽시계
경계(occurred_at)로 골라낸다.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection

#: 플레이어 개입 — 루프 3단계의 전 어휘 (plan/02)
INTERVENTION_TYPES = (
    "player.dm.sent",
    "player.comment.posted",
    "player.reaction.added",
    "player.follow.changed",
)
#: 응답 의무가 있는 개입 — 좋아요·팔로우는 스키마상 응답 의무가 없다 (분모 밖)
RESPONSE_ELIGIBLE_TYPES = ("player.dm.sent", "player.comment.posted")
RESPONSE_TYPE = "actor.message.sent"
POST_TYPE = "feed.post.published"

#: tick이 실린 재료(응답·포스트)는 retrospect처럼 tick 창으로 훑는다
_TICKED_SQL = (
    "SELECT type, event_id, occurred_at, causation_id, correlation_id, payload "
    "FROM es.events WHERE world_id = %s AND type = ANY(%s) AND tick >= %s AND tick < %s"
)

#: 시즌-일의 벽시계 시작 — 그 날 tick 스트림의 첫 적재 시각 (없으면 그 날은 안 돌았다)
_DAY_START_SQL = (
    "SELECT min(occurred_at) FROM es.events "
    "WHERE world_id = %s AND stream = 'system' AND stream_key = 'tick' "
    "AND tick >= %s AND tick < %s"
)

#: 플레이어 개입은 tick 0 규약 — 순서의 진실인 occurred_at으로 그 날을 고른다
_PLAYER_SQL = (
    "SELECT type, event_id, occurred_at, causation_id, correlation_id, payload "
    "FROM es.events WHERE world_id = %s AND type = ANY(%s) AND occurred_at >= %s"
)
_PLAYER_SQL_BOUNDED = _PLAYER_SQL + " AND occurred_at < %s"


def _when(envelope: dict[str, Any]) -> datetime:
    """봉투의 적재 시각 — SQL 행은 datetime, 리플레이/합성 봉투는 ISO 문자열."""
    raw = envelope["occurred_at"]
    if isinstance(raw, datetime):
        return raw
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _order_key(envelope: dict[str, Any]) -> tuple[datetime, str]:
    """이전/이후 판정 키 — 적재 시각, 동시각이면 ULID가 순서의 진실이다."""
    return (_when(envelope), envelope["event_id"])


def _percentile(samples: list[float], q: float) -> float:
    """최근접 순위(nearest-rank) 백분위 — 결정적, 보간 없음."""
    ranked = sorted(samples)
    return ranked[max(0, math.ceil(q * len(ranked)) - 1)]


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def fold_loop_health(envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    """봉투 열 → 루프 헬스 리포트. 순수·결정적 — 같은 봉투 열이면 같은 결과."""
    ordered = sorted(envelopes, key=lambda e: e["event_id"])
    interventions = [e for e in ordered if e["type"] in INTERVENTION_TYPES]
    by_type: dict[str, int] = {}
    players: set[str] = set()
    by_event_id: dict[str, dict[str, Any]] = {}
    chains: set[str] = set()
    for env in interventions:
        by_type[env["type"]] = by_type.get(env["type"], 0) + 1
        players.add(env["payload"]["player_id"])
        by_event_id[env["event_id"]] = env
        chains.add(env.get("correlation_id") or env["event_id"])

    # 응답 — causation이 이 창의 개입을 가리키는 플레이어 대상 메시지만.
    # 선제 DM(causation 없음)과 액터간 댓글(target_player 없음)은 응답이 아니다.
    responses = [
        e for e in ordered
        if e["type"] == RESPONSE_TYPE
        and e["payload"].get("target_player_id")
        and e.get("causation_id") in by_event_id
    ]
    first_response: dict[str, dict[str, Any]] = {}
    for resp in responses:
        cause = resp["causation_id"]
        if cause not in first_response or _order_key(resp) < _order_key(first_response[cause]):
            first_response[cause] = resp

    eligible = [e for e in interventions if e["type"] in RESPONSE_ELIGIBLE_TYPES]
    responded = [e for e in eligible if e["event_id"] in first_response]
    latencies = [
        (_when(first_response[e["event_id"]]) - _when(e)).total_seconds() for e in responded
    ]

    # 재개입 — 첫 응답을 받은 시점 이후의 개입만 루프 재회전이다
    first_response_at: dict[str, tuple[datetime, str]] = {}
    for resp in responses:
        player = resp["payload"]["target_player_id"]
        key = _order_key(resp)
        if player not in first_response_at or key < first_response_at[player]:
            first_response_at[player] = key
    reengaged = {
        env["payload"]["player_id"]
        for env in interventions
        if env["payload"]["player_id"] in first_response_at
        and _order_key(env) > first_response_at[env["payload"]["player_id"]]
    }

    # 드라마 재생산 — 개입의 correlation 사슬을 승계한 포스트만 (남의 사슬은 무관)
    derived_posts = sum(
        1 for e in ordered if e["type"] == POST_TYPE and e.get("correlation_id") in chains
    )

    return {
        "interventions": {
            "total": len(interventions),
            "by_type": dict(sorted(by_type.items())),
            "players": len(players),
        },
        "responses": {
            "eligible_interventions": len(eligible),
            "responded_interventions": len(responded),
            "response_rate": _ratio(len(responded), len(eligible)),
            "matched_responses": len(responses),
            "latency_seconds": {
                "p50": round(_percentile(latencies, 0.5), 3) if latencies else None,
                "p95": round(_percentile(latencies, 0.95), 3) if latencies else None,
                "samples": len(latencies),
            },
        },
        "reengagement": {
            "responded_players": len(first_response_at),
            "reengaged_players": len(reengaged),
            "rate": _ratio(len(reengaged), len(first_response_at)),
        },
        "drama_reproduction": {
            "derived_posts": derived_posts,
            "rate": _ratio(derived_posts, len(interventions)),
        },
    }


def _envelope(row: tuple) -> dict[str, Any]:
    return {
        "type": row[0], "event_id": row[1], "occurred_at": row[2],
        "causation_id": row[3], "correlation_id": row[4], "payload": row[5],
    }


async def loop_health(
    conn: AsyncConnection, world_id: str, day: int, *, interval_ticks: int = 360
) -> dict[str, Any]:
    """시즌-일(day) 하나의 루프 헬스 — season_retrospective와 동형의 창 조회.

    응답·포스트는 tick 창으로, 플레이어 개입(tick 0 규약)은 그 날 tick
    스트림의 벽시계 경계로 고른다. 그 날 tick이 없으면 벽시계 창을 잡을 수
    없어 개입은 비고(영값), 다음 날 tick이 아직 없으면 위가 열린 창이다.
    """
    lo, hi = day * interval_ticks, (day + 1) * interval_ticks
    rows = await (
        await conn.execute(_TICKED_SQL, (world_id, [RESPONSE_TYPE, POST_TYPE], lo, hi))
    ).fetchall()
    envelopes = [_envelope(row) for row in rows]

    (day_start,) = await (await conn.execute(_DAY_START_SQL, (world_id, lo, hi))).fetchone()
    day_end = None
    if day_start is not None:
        (day_end,) = await (
            await conn.execute(_DAY_START_SQL, (world_id, hi, hi + interval_ticks))
        ).fetchone()
        types = list(INTERVENTION_TYPES)
        if day_end is None:
            cur = await conn.execute(_PLAYER_SQL, (world_id, types, day_start))
        else:
            cur = await conn.execute(_PLAYER_SQL_BOUNDED, (world_id, types, day_start, day_end))
        envelopes += [_envelope(row) for row in await cur.fetchall()]

    report = fold_loop_health(envelopes)
    report["world_id"] = world_id
    report["day"] = day
    report["tick_range"] = [lo, hi]
    return report
