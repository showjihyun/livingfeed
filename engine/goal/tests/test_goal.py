"""Goal Engine 순수 로직 검증 (ADR-012) — 결정적, 리플레이 재현.

행동 → 욕구 충족 + 목표 진행, 임계 발행, 감쇠, 가장 목마른 욕구.
"""

from lf_goal import (
    GoalState,
    appraise_action,
    arc_focus_need,
    decay,
    describe,
    initial_state,
    pressing_need,
    satisfy_from_interaction,
    starvation,
)

MINJI_GOALS = [
    {"id": "g_decide_resignation", "description": "퇴사 결정",
     "priority": 0.9, "need": "security"},
    {"id": "g_side_project", "description": "사이드 프로젝트 완성",
     "priority": 0.6, "need": "achievement"},
]
MINJI_BIAS = {"achievement": 0.55, "belonging": 0.85, "security": 0.60}


def test_initial_state_fills_needs_and_zero_goals():
    state = initial_state(MINJI_GOALS, MINJI_BIAS)
    assert set(state.goals) == {"g_decide_resignation", "g_side_project"}
    assert all(v == 0.0 for v in state.goals.values())
    # 소속 편향(0.85)이 가장 크니 초기 만족도도 가장 높다
    assert state.needs["belonging"] > state.needs["achievement"]


def test_aligned_action_advances_matching_goal_only():
    state = initial_state(MINJI_GOALS, MINJI_BIAS)
    result = appraise_action(state, "work", MINJI_GOALS, MINJI_BIAS)
    # work=achievement → 사이드 프로젝트만 진행, 퇴사(security)는 그대로
    assert result.state.goals["g_side_project"] > 0
    assert result.state.goals["g_decide_resignation"] == 0
    assert result.state.needs["achievement"] > state.needs["achievement"]
    assert result.congruence == 0.55  # achievement 관심도


def test_unmapped_action_is_neutral():
    state = initial_state(MINJI_GOALS, MINJI_BIAS)
    result = appraise_action(state, "daydream", MINJI_GOALS, MINJI_BIAS)
    assert result.congruence == 0.0
    assert result.state == state


def test_progress_publishes_only_past_threshold():
    state = initial_state(MINJI_GOALS, MINJI_BIAS)
    # 한 걸음(0.12·0.6=0.072)은 임계(0.2) 미만 — 침묵
    r1 = appraise_action(state, "work", MINJI_GOALS, MINJI_BIAS)
    assert r1.advances == []
    # 누적으로 임계를 넘으면 발행
    r = r1
    fired = False
    for _ in range(4):
        r = appraise_action(r.state, "work", MINJI_GOALS, MINJI_BIAS)
        if r.advances:
            fired = True
            break
    assert fired
    [adv] = r.advances
    assert adv.goal_id == "g_side_project"
    assert adv.need == "achievement"
    assert adv.progress > 0.2


def test_goal_completes_once_and_marks_achieved():
    # 높은 우선순위 목표를 반복 행동으로 완주시킨다
    goals = [{"id": "g_x", "description": "특종", "priority": 1.0, "need": "achievement"}]
    bias = {"achievement": 0.9, "belonging": 0.4, "security": 0.3}
    state = initial_state(goals, bias)
    achieved_events = []
    for _ in range(20):
        result = appraise_action(state, "work", goals, bias)
        state = result.state
        achieved_events += [a for a in result.advances if a.achieved]
        if state.goals["g_x"] >= 1.0:
            break
    assert state.goals["g_x"] == 1.0
    assert len(achieved_events) == 1  # 완주는 딱 한 번 마디가 된다
    assert achieved_events[0].progress == 1.0

    # 이미 이룬 목표는 더 진행하지도, 재발행하지도 않는다 (스팸 없음)
    after = appraise_action(state, "work", goals, bias)
    assert after.state.goals["g_x"] == 1.0
    assert not any(a.goal_id == "g_x" for a in after.advances)


def test_interaction_fills_belonging_without_goal_progress():
    state = initial_state(MINJI_GOALS, MINJI_BIAS)
    after = satisfy_from_interaction(state, "player.dm.sent")
    assert after.needs["belonging"] > state.needs["belonging"]
    assert after.goals == state.goals  # 관심은 위안이지 성취가 아니다


def test_decay_returns_needs_toward_deprivation():
    state = GoalState(needs={"achievement": 0.5, "belonging": 0.5, "security": 0.5}, goals={})
    after = decay(state, ticks=2)
    assert all(after.needs[n] < 0.5 for n in after.needs)
    assert after.goals == state.goals  # 목표는 감쇠하지 않는다


def test_pressing_need_is_deprived_and_cared():
    # 소속을 많이 채웠으면, 결핍된 안정(security)이 더 목마르다
    state = GoalState(
        needs={"achievement": 0.2, "belonging": 0.9, "security": 0.1}, goals={}
    )
    assert pressing_need(state, MINJI_BIAS) == "security"


def test_describe_names_pressing_need_and_top_goal():
    state = initial_state(MINJI_GOALS, MINJI_BIAS)
    state = appraise_action(state, "work", MINJI_GOALS, MINJI_BIAS).state
    text = describe(state, MINJI_GOALS, MINJI_BIAS)
    assert "목마른" in text
    assert "사이드 프로젝트" in text


def test_arc_focus_reorders_and_marks_aligned_goal():
    """아크가 미는 욕구의 목표가 앞자리 + 표식 — 인생의 장이 목표 순서로 스민다
    (plan/08 전환점 사슬). 목록은 그대로, 순서와 강조만 바뀐다."""
    state = initial_state(MINJI_GOALS, MINJI_BIAS)
    # work로 사이드 프로젝트(achievement)가 진행됨 — 진행도 기준이면 이게 앞이다
    state = appraise_action(state, "work", MINJI_GOALS, MINJI_BIAS).state
    plain = describe(state, MINJI_GOALS, MINJI_BIAS)
    assert plain.index("사이드 프로젝트") < plain.index("퇴사 결정")

    # settling(정착기) 아크 → security를 민다 → 퇴사 결정이 앞자리로 온다
    focused = describe(state, MINJI_GOALS, MINJI_BIAS, focus_need=arc_focus_need("settling"))
    assert focused.index("퇴사 결정") < focused.index("사이드 프로젝트")
    assert "퇴사 결정" in focused.split("마음이 기우는 곳")[0]  # 표식이 정렬 목표에 붙는다
    # 미지 단계·아크 없음 — 재배열 없음
    assert arc_focus_need("wanderer") is None and arc_focus_need(None) is None


def test_starvation_fires_for_deprived_cared_need():
    # 소속(관심 0.85)이 바닥 → 좌절 신호
    state = GoalState(needs={"achievement": 0.5, "belonging": 0.05, "security": 0.5}, goals={})
    signal = starvation(state, MINJI_BIAS)
    assert signal is not None
    need, deficit = signal
    assert need == "belonging"
    assert deficit > 0.9


def test_starvation_silent_when_satisfied_or_uncared():
    # 채워져 있으면 좌절 없음
    full = GoalState(needs={"achievement": 0.6, "belonging": 0.6, "security": 0.6}, goals={})
    assert starvation(full, MINJI_BIAS) is None
    # 관심 없는 욕구(achievement 0.1)만 바닥이면 태연하다
    low_care_bias = {"achievement": 0.1, "belonging": 0.2, "security": 0.2}
    deprived = GoalState(needs={"achievement": 0.0, "belonging": 0.9, "security": 0.9}, goals={})
    assert starvation(deprived, low_care_bias) is None
