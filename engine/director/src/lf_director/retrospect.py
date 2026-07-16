"""시즌 회고 — 리플레이 기반 서사 품질 리포트 (ADR-013 후속).

이벤트 스토어의 감사·시즌·아크·사건 스트림을 접어, 시즌(세계 하루) 동안
Director가 어떤 연출을 했는지 정량 요약한다: 개입이 기계적이었나(도구
다양성·최장 연속), 세계를 얼마나 흔들었나(사건 강도), 장이 몇 번
넘어갔나(아크 전환). 튜닝의 눈이다 — 수치가 파라미터 조정의 근거가 된다.

fold_report는 순수 함수다: 같은 봉투 열 → 같은 리포트 (리플레이 재현).
"""

from __future__ import annotations

from typing import Any

from lf_eventstore import read_stream
from psycopg import AsyncConnection

from lf_director.rules import ARC_TOOL, ARC_TYPE, SEASON_TOOL, SEASON_TYPE

AUDIT_TYPE = "system.director.intervened"
INCIDENT_TYPE = "world.incident.occurred"
POST_TYPE = "feed.post.published"

#: 회고가 읽는 스트림들 — Director 연출의 전 산출물 (감사·시즌·아크·사건)
_SOURCES = (
    ("system", "director"),
    ("system", "season"),
    ("system", "arc"),
    ("world", "incidents"),
)

#: 피드 포스트는 포스트별 스트림(stream_key=post_id)이라 read_stream 열거가 불가 —
#: 서사 품질 지표(내레이션 비율 등)를 위해 read-only SELECT로 tick 창을 훑는다
_FEED_SQL = (
    "SELECT payload FROM es.events "
    "WHERE world_id = %s AND type = 'feed.post.published' AND tick >= %s AND tick < %s"
)


def fold_report(envelopes: list[dict[str, Any]]) -> dict[str, Any]:
    """봉투 열(시간순) → 시즌 리포트. 순수·결정적 — 리플레이가 곧 회고다."""
    by_tool: dict[str, int] = {}
    by_selector: dict[str, int] = {}
    incident_kinds: dict[str, int] = {}
    intensities: list[float] = []
    themes: list[str] = []
    arc_stage: dict[str, str] = {}
    arc_plans = 0
    arc_transitions = 0
    max_streak = 0
    streak = 0
    last_drama_tool: str | None = None

    for envelope in sorted(envelopes, key=lambda e: e["event_id"]):
        etype = envelope["type"]
        payload = envelope["payload"]
        if etype == AUDIT_TYPE:
            tool = payload["tool"]
            by_tool[tool] = by_tool.get(tool, 0) + 1
            selector = (payload.get("signals") or {}).get("selector", "rule")
            by_selector[selector] = by_selector.get(selector, 0) + 1
            if tool not in (SEASON_TOOL, ARC_TOOL):  # 드라마 도구만 연속성을 센다
                streak = streak + 1 if tool == last_drama_tool else 1
                max_streak = max(max_streak, streak)
                last_drama_tool = tool
        elif etype == SEASON_TYPE:
            themes.append(payload["theme"])
        elif etype == ARC_TYPE:
            arc_plans += 1
            target = payload["target_actor_id"]
            if arc_stage.get(target) != payload["stage"]:
                arc_transitions += 1  # 첫 아크도 장이 열린 것이다
            arc_stage[target] = payload["stage"]
        elif etype == INCIDENT_TYPE:
            kind = payload["incident_kind"]
            incident_kinds[kind] = incident_kinds.get(kind, 0) + 1
            intensities.append(float(payload.get("intensity", 0.0)))

    posts = 0
    llm_narrated = 0
    transition_posts = 0
    post_drama: list[float] = []
    for envelope in envelopes:
        if envelope["type"] != POST_TYPE:
            continue
        payload = envelope["payload"]
        posts += 1
        if payload.get("narration_kind") == "llm":
            llm_narrated += 1
        if "arc_transition" in payload.get("tags", []):
            transition_posts += 1
        post_drama.append(float(payload.get("drama_score", 0.0)))

    drama_total = sum(
        n for tool, n in by_tool.items() if tool not in (SEASON_TOOL, ARC_TOOL)
    )
    drama_distinct = len(
        [t for t in by_tool if t not in (SEASON_TOOL, ARC_TOOL)]
    )
    return {
        "interventions": {
            "total": sum(by_tool.values()),
            "drama": drama_total,
            "by_tool": dict(sorted(by_tool.items())),
            "by_selector": dict(sorted(by_selector.items())),
        },
        "diversity": {
            # 1.0 = 쓴 만큼 전부 다른 도구, 낮을수록 기계적 — 튜닝 지표
            "tool_diversity": (
                round(drama_distinct / drama_total, 4) if drama_total else None
            ),
            "max_tool_streak": max_streak,
            "incident_kinds": dict(sorted(incident_kinds.items())),
        },
        "world": {
            "incidents": len(intensities),
            "avg_intensity": (
                round(sum(intensities) / len(intensities), 4) if intensities else None
            ),
        },
        "season": {"themes_set": themes, "final_theme": themes[-1] if themes else None},
        "arcs": {"planned": arc_plans, "transitions": arc_transitions},
        # 서사 품질 — 그 날 피드에 오른 이야기의 결 (편집 산출물 관점)
        "narrative": {
            "posts": posts,
            "llm_narration_ratio": (
                round(llm_narrated / posts, 4) if posts else None
            ),
            "arc_transition_posts": transition_posts,
            "avg_post_drama": (
                round(sum(post_drama) / len(post_drama), 4) if post_drama else None
            ),
        },
    }


def tune_pacing(
    report: dict[str, Any], current_quiet_to_fire: int, params: dict[str, Any]
) -> tuple[int, str | None]:
    """회고 → 페이싱 조정 (비례 제어의 첫 절단면, ADR-013 회고 후속). 순수·결정적.

    지난 날 드라마 개입이 목표보다 적었으면 세계가 조용했던 것 — 침체를 덜
    참게(quiet_ticks_to_fire ↓), 많았으면 더 참게(↑) 한 걸음 움직인다.
    반환: (새 값, 사유) — 조정 없음이면 (현재 값, None). 클램프가 폭주를 막는다.
    """
    cfg = params.get("retrospect_tuning")
    if not cfg:
        return current_quiet_to_fire, None
    drama = int(report["interventions"]["drama"])
    target = int(cfg["target_interventions_per_day"])
    step = int(cfg["adjust_step"])
    if drama < target:
        proposed = current_quiet_to_fire - step
        reason = f"조용했다 (개입 {drama} < 목표 {target}) — 침체를 덜 참는다"
    elif drama > target:
        proposed = current_quiet_to_fire + step
        reason = f"북적였다 (개입 {drama} > 목표 {target}) — 더 참는다"
    else:
        return current_quiet_to_fire, None
    clamped = max(int(cfg["quiet_ticks_min"]), min(int(cfg["quiet_ticks_max"]), proposed))
    if clamped == current_quiet_to_fire:
        return current_quiet_to_fire, None  # 이미 벽에 붙어 있다 — 더 갈 곳이 없다
    return clamped, reason


async def season_retrospective(
    conn: AsyncConnection, world_id: str, day: int, *, interval_ticks: int = 360
) -> dict[str, Any]:
    """시즌-일(day) 하나의 회고 — es 스트림에서 그 날의 연출 산출물을 모아 접는다."""
    lo, hi = day * interval_ticks, (day + 1) * interval_ticks
    envelopes: list[dict[str, Any]] = []
    for stream, key in _SOURCES:
        for stored in await read_stream(conn, world_id, stream, key):
            envelope = stored.envelope
            if lo <= envelope["tick"] < hi:
                envelopes.append(envelope)
    # 피드 포스트(서사 품질 재료) — 포스트별 스트림이라 SQL로 tick 창을 훑는다
    rows = await (await conn.execute(_FEED_SQL, (world_id, lo, hi))).fetchall()
    envelopes += [
        {"type": POST_TYPE, "event_id": "", "tick": lo, "payload": payload}
        for (payload,) in rows
    ]
    report = fold_report(envelopes)
    report["world_id"] = world_id
    report["day"] = day
    report["tick_range"] = [lo, hi]
    return report
