"""개입 규칙 검증 — 임계·예산·순환 (ADR-013 hard rule)."""

from lf_director.rules import BudgetState, decide
from lf_director.signals import Snapshot, default_params

PARAMS = default_params()
FIRE = PARAMS["observation"]["quiet_ticks_to_fire"]


def quiet_snapshot(tick: int = 100, quiet: int | None = None) -> Snapshot:
    return Snapshot(tick=tick, drama_ma=0.05, quiet_ticks=quiet if quiet is not None else FIRE)


def test_no_intervention_below_threshold():
    assert decide(quiet_snapshot(quiet=FIRE - 1), BudgetState(), []) is None


def test_fires_at_threshold_and_rotates_tools_with_tension():
    """긴장 후보가 있으면 규칙 경로도 도구를 순환한다 — 규칙 폴백의 다양성 (ADR-013).

    넷 다 화이트리스트 안이고, 도구별 payload가 제 모양이어야 한다.
    """
    tension = [["a_junho_park", "a_aria_kim", 0.42, -0.1]]
    budget = BudgetState()
    seen = {}
    for i in range(8):
        intervention = decide(quiet_snapshot(tick=100 + i * 200), budget, tension)
        assert intervention is not None
        assert "침체 감지" in intervention.reason
        assert intervention.signals["tension_top"] == tension
        seen[intervention.tool] = intervention
        budget.record(100 + i * 200, None)
    assert set(seen) == {
        "inject_incident", "nudge_perception", "promote_actor", "boost_feed"
    }

    incident = seen["inject_incident"]
    assert incident.event_type == "world.incident.occurred"
    # 갈등 후보 쌍이 사건의 영향권에 놓인다 (그래프 질의 → 무대 배치, ADR-006/013)
    assert incident.payload["affected_actor_ids"] == ["a_junho_park", "a_aria_kim"]
    nudge = seen["nudge_perception"]
    assert nudge.event_type == "world.observation.surfaced"
    assert nudge.payload["target_actor_id"] == "a_junho_park"
    assert nudge.payload["about_actor_id"] == "a_aria_kim"
    assert nudge.payload["observation"]  # 관측 템플릿이 채워졌다
    promote = seen["promote_actor"]
    assert promote.stream == "system"  # 세계 사건이 아니라 제어 신호
    assert promote.payload == {"target_actor_id": "a_junho_park"}
    boost = seen["boost_feed"]
    assert boost.event_type == "system.director.feed_boosted"
    assert boost.payload["target_actor_id"] == "a_junho_park"
    assert 0 < boost.payload["boost"] <= 1  # 강도·기간은 노브다
    assert boost.payload["until_tick"] > 100  # 조명은 영원하지 않다


def test_no_tension_means_incident_only():
    # 긴장 후보가 없으면 대상 있는 도구(nudge/promote)를 만들 수 없다 — incident뿐
    budget = BudgetState()
    for i in range(4):
        intervention = decide(quiet_snapshot(tick=100 + i * 200), budget, [])
        assert intervention is not None and intervention.tool == "inject_incident"
        budget.record(100 + i * 200, None)


def test_rule_nudge_grounds_observation_with_name():
    # 관측 템플릿이 상대 이름으로 그라운딩된다 — id가 노출되지 않는다
    tension = [["a_junho_park", "a_aria_kim", 0.42, -0.1]]
    budget = BudgetState()
    for i in range(6):
        intervention = decide(
            quiet_snapshot(tick=100 + i * 200), budget, tension,
            names={"a_aria_kim": "김아리"},
        )
        budget.record(100 + i * 200, None)
        if intervention.tool == "nudge_perception":
            assert "김아리" in intervention.payload["observation"]
            assert "a_aria_kim" not in intervention.payload["observation"]
            return
    raise AssertionError("nudge가 도구 순환에 나타나지 않았다")


def test_budget_caps_interventions_per_window():
    budget = BudgetState()
    window = PARAMS["budget"]["window_ticks"]
    limit = PARAMS["budget"]["max_interventions"]

    fired = 0
    for i in range(limit + 3):
        if decide(quiet_snapshot(tick=100 + i), budget, []) is not None:
            budget.record(100 + i, None)
            fired += 1
    assert fired == limit  # 창당 상한 (hard rule)

    # 창이 지나면 예산이 회복된다
    assert decide(quiet_snapshot(tick=100 + window + 1), budget, []) is not None


def test_incident_kinds_rotate_deterministically():
    budget = BudgetState()
    kinds = []
    for i in range(4):
        intervention = decide(quiet_snapshot(tick=i * 200), budget, [])
        assert intervention is not None
        kinds.append(intervention.payload["incident_kind"])
        budget.record(i * 200, None)
    assert len(set(kinds)) > 1  # 같은 도구·같은 사건 반복의 기계감 방지


def test_decide_avoids_last_incident_kind():
    # 직전 사건 종류와 순환 선택이 겹치면 다음 종류로 비킨다 (다양성 감점 — 결정적)
    budget = BudgetState()
    first = decide(quiet_snapshot(), budget, [])
    assert first is not None
    same = first.payload["incident_kind"]
    avoided = decide(quiet_snapshot(), budget, [], avoid_kind=same)
    assert avoided is not None
    assert avoided.payload["incident_kind"] != same
    # 다른 종류를 피하라면 순환 선택 그대로 (겹치지 않으면 비킬 이유가 없다)
    untouched = decide(quiet_snapshot(), budget, [], avoid_kind="__none__")
    assert untouched is not None and untouched.payload["incident_kind"] == same
