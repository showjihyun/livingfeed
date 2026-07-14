"""개입 규칙 검증 — 임계·예산·순환 (ADR-013 hard rule)."""

from lf_director.rules import BudgetState, decide
from lf_director.signals import Snapshot, default_params

PARAMS = default_params()
FIRE = PARAMS["observation"]["quiet_ticks_to_fire"]


def quiet_snapshot(tick: int = 100, quiet: int | None = None) -> Snapshot:
    return Snapshot(tick=tick, drama_ma=0.05, quiet_ticks=quiet if quiet is not None else FIRE)


def test_no_intervention_below_threshold():
    assert decide(quiet_snapshot(quiet=FIRE - 1), BudgetState(), []) is None


def test_fires_at_threshold_with_tension_targets():
    tension = [["a_junho_park", "a_aria_kim", 0.42, -0.1]]
    intervention = decide(quiet_snapshot(), BudgetState(), tension)
    assert intervention is not None
    assert intervention.tool == "inject_incident"  # 화이트리스트 밖은 존재하지 않는다
    assert intervention.event_type == "world.incident.occurred"
    # 갈등 후보 쌍이 사건의 영향권에 놓인다 (그래프 질의 → 무대 배치, ADR-006/013)
    assert intervention.payload["affected_actor_ids"] == ["a_junho_park", "a_aria_kim"]
    assert "침체 감지" in intervention.reason
    assert intervention.signals["tension_top"] == tension


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
