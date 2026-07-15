"""시즌 회고 검증 — 봉투 열 → 서사 품질 리포트 (ADR-013 후속).

fold_report는 순수·결정적: 같은 봉투 열 → 같은 리포트 (리플레이 = 회고).
PG 통합은 season_retrospective가 그 날의 스트림만 접는지 확인한다.
"""

from lf_director.retrospect import fold_report, season_retrospective
from lf_eventstore import NewEvent, append

WORLD = "w_test"


def audit(event_id: str, tick: int, tool: str, *, selector: str = "llm", kind=None) -> dict:
    signals: dict = {"selector": selector}
    if kind is not None:
        signals["incident_kind"] = kind
    return {
        "type": "system.director.intervened", "event_id": event_id, "tick": tick,
        "payload": {"tool": tool, "signals": signals},
    }


def test_fold_report_measures_diversity_and_streak():
    envelopes = [
        audit("01A", 10, "inject_incident", kind="rumor_spread"),
        audit("01B", 20, "inject_incident", kind="rumor_spread"),
        audit("01C", 30, "nudge_perception", selector="rule"),
        audit("01D", 40, "set_season_theme"),  # 저빈도 — 드라마 연속성에 안 낀다
        audit("01E", 50, "inject_incident", kind="blackout"),
        {"type": "world.incident.occurred", "event_id": "01F", "tick": 10,
         "payload": {"incident_kind": "rumor_spread", "intensity": 0.6}},
        {"type": "world.incident.occurred", "event_id": "01G", "tick": 50,
         "payload": {"incident_kind": "blackout", "intensity": 0.8}},
        {"type": "system.director.season_set", "event_id": "01H", "tick": 40,
         "payload": {"theme": "turmoil"}},
    ]
    report = fold_report(envelopes)
    assert report["interventions"]["total"] == 5
    assert report["interventions"]["drama"] == 4
    assert report["interventions"]["by_tool"]["inject_incident"] == 3
    assert report["interventions"]["by_selector"] == {"llm": 4, "rule": 1}
    # 드라마 도구 4회에 2종 — 다양성 0.5, 최장 연속은 incident 2연속
    assert report["diversity"]["tool_diversity"] == 0.5
    assert report["diversity"]["max_tool_streak"] == 2
    assert report["diversity"]["incident_kinds"] == {"blackout": 1, "rumor_spread": 1}
    assert report["world"]["incidents"] == 2
    assert report["world"]["avg_intensity"] == 0.7
    assert report["season"]["final_theme"] == "turmoil"


def test_fold_report_counts_arc_transitions_per_actor():
    def arc(event_id, tick, target, stage):
        return {"type": "system.director.arc_planned", "event_id": event_id, "tick": tick,
                "payload": {"target_actor_id": target, "stage": stage, "intention": "x"}}

    report = fold_report([
        arc("01A", 10, "a_minji", "settling"),   # 첫 장 — 전환
        arc("01B", 20, "a_minji", "settling"),   # 같은 장 재계획 — 전환 아님
        arc("01C", 30, "a_minji", "prime"),      # 장이 넘어갔다
        arc("01D", 40, "a_aria", "newcomer"),    # 다른 인물의 첫 장
    ])
    assert report["arcs"] == {"planned": 4, "transitions": 3}


def test_fold_report_is_order_insensitive_and_empty_safe():
    # 봉투가 뒤섞여 들어와도 event_id 정렬로 같은 리포트 (리플레이 재현)
    a = audit("01A", 10, "inject_incident")
    b = audit("01B", 20, "nudge_perception")
    assert fold_report([a, b]) == fold_report([b, a])
    empty = fold_report([])
    assert empty["interventions"]["total"] == 0
    assert empty["diversity"]["tool_diversity"] is None
    assert empty["world"]["avg_intensity"] is None


async def test_season_retrospective_folds_only_that_day(conn):
    """그 날(tick 창)의 산출물만 접는다 — 다른 날의 개입은 리포트 밖이다."""
    async def seed_audit(event_id: str, tick: int, tool: str) -> None:
        head_key = "director"
        from lf_eventstore import current_head
        head = await current_head(conn, WORLD, "system", head_key)
        await append(
            conn, "engine.director",
            [NewEvent(
                world_id=WORLD, stream="system", stream_key=head_key,
                type="system.director.intervened", tick=tick, event_id=event_id,
                payload={"tool": tool, "reason": "회고 시드",
                         "signals": {"selector": "rule"},
                         "target_correlation_id": None, "budget_remaining": 1},
            )],
            expected_head=head,
        )

    ulid = "01JZK7Q3W000000000000000"  # 24자 + 2자 suffix로 26자 ULID
    await seed_audit(ulid + "0A", 10, "inject_incident")     # day 0
    await seed_audit(ulid + "0B", 350, "nudge_perception")   # day 0
    await seed_audit(ulid + "0C", 400, "promote_actor")      # day 1 — 창 밖

    report = await season_retrospective(conn, WORLD, day=0, interval_ticks=360)
    assert report["interventions"]["total"] == 2
    assert set(report["interventions"]["by_tool"]) == {"inject_incident", "nudge_perception"}
    assert report["tick_range"] == [0, 360]

    next_day = await season_retrospective(conn, WORLD, day=1, interval_ticks=360)
    assert next_day["interventions"]["by_tool"] == {"promote_actor": 1}
