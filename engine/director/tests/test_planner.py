"""LLM 개입 선택의 순수 로직 — 도구 선택 + 하드룰 클램핑이 핵심 (ADR-013).

LLM은 행동 반경을 넓힐 수 없다: 화이트리스트 밖 도구·사건은 거부(폴백), 환각 액터는
후보 밖이면 버린다, location은 라이브러리에서만, intensity는 [0,1]. 결정적.
"""

from lf_director.planner import (
    build_plan_user,
    build_season_user,
    candidate_actor_ids,
    intervention_from_plan,
    plan_schema,
    season_from_plan,
    season_schema,
)
from lf_director.signals import Snapshot

INCIDENTS = [
    {"kind": "chance_encounter", "description": "카페 정전",
     "location_id": "loc_cafe", "intensity": 0.65},
    {"kind": "rumor_spread", "description": "출처 불명의 소문",
     "location_id": "loc_office", "intensity": 0.55},
]
TENSION = [["a_minji", "a_seongho", 0.8, 0.2], ["a_aria", "a_junho", 0.5, 0.3]]
NAMES = {"a_minji": "김민지", "a_seongho": "박성호"}
SNAP = Snapshot(tick=120, drama_ma=0.05, quiet_ticks=30)
THEMES = [
    {"name": "calm", "quiet_ticks_to_fire": 45, "max_interventions": 1},
    {"name": "turmoil", "quiet_ticks_to_fire": 12, "max_interventions": 4},
]


def test_plan_schema_lists_only_drama_tools():
    # 드라마 게이트 도구는 셋 — set_season_theme은 저빈도 시즌 계획으로 분리됐다
    schema = plan_schema(["chance_encounter", "rumor_spread"])
    assert schema["properties"]["tool"]["enum"] == [
        "inject_incident", "nudge_perception", "promote_actor"
    ]
    assert schema["properties"]["incident_kind"]["enum"] == ["chance_encounter", "rumor_spread"]
    assert "season_theme" not in schema["properties"]  # 드라마 스키마엔 없다
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"tool", "rationale"}


def test_season_schema_lists_themes():
    schema = season_schema(["calm", "turmoil"])
    assert schema["properties"]["season_theme"]["enum"] == ["calm", "turmoil"]
    assert set(schema["required"]) == {"season_theme", "rationale"}
    assert schema["additionalProperties"] is False


def test_candidate_actor_ids_are_distinct_in_order():
    assert candidate_actor_ids(TENSION) == ["a_minji", "a_seongho", "a_aria", "a_junho"]
    assert candidate_actor_ids([]) == []


# --- inject_incident 도구 -----------------------------------------------------


def test_valid_incident_keeps_llm_framing_and_records_selector():
    plan = {
        "tool": "inject_incident",
        "incident_kind": "rumor_spread",
        "affected_actor_ids": ["a_minji"],
        "description": "박성호를 둘러싼 말이 김민지 귀에까지 닿았다",
        "intensity": 0.7,
        "rationale": "둘의 원한이 가장 높다 — 소문이 불씨가 된다",
    }
    result = intervention_from_plan(plan, SNAP, TENSION, INCIDENTS, NAMES, model="qwen3:8b")
    assert result is not None
    assert result.tool == "inject_incident"
    assert result.event_type == "world.incident.occurred"
    assert result.stream_key == "incidents"
    assert result.payload["incident_kind"] == "rumor_spread"
    assert result.payload["description"] == plan["description"]  # LLM의 맥락 서술 보존
    assert result.payload["intensity"] == 0.7
    assert result.payload["affected_actor_ids"] == ["a_minji"]
    assert result.payload["location_id"] == "loc_office"  # 라이브러리에서만 — LLM이 못 지어낸다
    assert result.reason == plan["rationale"]
    assert result.signals["selector"] == "llm"
    assert result.signals["model"] == "qwen3:8b"


def test_unknown_tool_returns_none_for_fallback():
    plan = {"tool": "mind_control", "rationale": "x"}  # 화이트리스트 밖 도구
    assert intervention_from_plan(plan, SNAP, TENSION, INCIDENTS, NAMES) is None


def test_unknown_incident_kind_returns_none_for_fallback():
    plan = {
        "tool": "inject_incident",
        "incident_kind": "earthquake",  # 라이브러리 밖 — 존재하지 않는다
        "affected_actor_ids": ["a_minji"],
        "description": "지진",
        "intensity": 0.9,
        "rationale": "x",
    }
    assert intervention_from_plan(plan, SNAP, TENSION, INCIDENTS, NAMES) is None


def test_hallucinated_actors_dropped_falls_back_to_top_pair():
    plan = {
        "tool": "inject_incident",
        "incident_kind": "chance_encounter",
        "affected_actor_ids": ["a_ghost", "a_nobody"],  # 후보 밖 — 전부 버려진다
        "description": "우연한 마주침",
        "intensity": 0.6,
        "rationale": "x",
    }
    result = intervention_from_plan(plan, SNAP, TENSION, INCIDENTS, NAMES)
    assert result is not None
    assert result.payload["affected_actor_ids"] == ["a_minji", "a_seongho"]  # 상위 긴장 쌍 폴백


def test_partial_hallucination_keeps_only_valid_candidates():
    plan = {
        "tool": "inject_incident",
        "incident_kind": "chance_encounter",
        "affected_actor_ids": ["a_minji", "a_ghost"],
        "description": "우연한 마주침",
        "intensity": 0.6,
        "rationale": "x",
    }
    result = intervention_from_plan(plan, SNAP, TENSION, INCIDENTS, NAMES)
    assert result is not None
    assert result.payload["affected_actor_ids"] == ["a_minji"]  # 환각만 제거, 유효는 유지


def test_intensity_is_clamped_and_defaults_to_library():
    base = {
        "tool": "inject_incident",
        "incident_kind": "chance_encounter",
        "affected_actor_ids": ["a_minji"],
        "description": "x",
        "rationale": "x",
    }

    def intensity_of(plan: dict) -> float:
        return intervention_from_plan(plan, SNAP, TENSION, INCIDENTS, NAMES).payload["intensity"]

    assert intensity_of({**base, "intensity": 5.0}) == 1.0
    assert intensity_of({**base, "intensity": -3}) == 0.0
    # 없거나 숫자가 아니면 라이브러리 기본 강도
    assert intensity_of(base) == 0.65
    assert intensity_of({**base, "intensity": "높음"}) == 0.65


def test_empty_description_falls_back_to_library():
    plan = {
        "tool": "inject_incident",
        "incident_kind": "chance_encounter",
        "affected_actor_ids": ["a_minji"],
        "description": "   ",
        "intensity": 0.6,
        "rationale": "x",
    }
    result = intervention_from_plan(plan, SNAP, TENSION, INCIDENTS, NAMES)
    assert result.payload["description"] == "카페 정전"  # 라이브러리 서술로 폴백


def test_no_tension_yields_pure_environmental_incident():
    plan = {
        "tool": "inject_incident",
        "incident_kind": "chance_encounter",
        "affected_actor_ids": ["a_minji"],  # 후보 없음 → 전부 버려진다
        "description": "정전",
        "intensity": 0.6,
        "rationale": "x",
    }
    result = intervention_from_plan(plan, SNAP, [], INCIDENTS, NAMES)
    assert result is not None
    assert result.payload["affected_actor_ids"] == []  # 영향권 없는 순수 환경 사건도 유효


# --- nudge_perception 도구 ----------------------------------------------------


def test_valid_nudge_delivers_private_observation():
    plan = {
        "tool": "nudge_perception",
        "target_actor_id": "a_minji",
        "observation": "박성호가 어제 한 말과 앞뒤가 안 맞는 메모를 보게 됐다",
        "about_actor_id": "a_seongho",
        "rationale": "공개 소동 없이 민지의 다음 선택을 흔든다",
    }
    result = intervention_from_plan(plan, SNAP, TENSION, INCIDENTS, NAMES, model="qwen3:8b")
    assert result is not None
    assert result.tool == "nudge_perception"
    assert result.event_type == "world.observation.surfaced"  # 비공개 — 피드 승격 안 됨
    assert result.stream_key == "observations"
    assert result.payload["target_actor_id"] == "a_minji"
    assert result.payload["observation"] == plan["observation"]
    assert result.payload["about_actor_id"] == "a_seongho"
    assert result.signals["selector"] == "llm"


def test_nudge_target_outside_candidates_returns_none():
    # 없는 대상에게는 관측을 배달할 수 없다 — 규칙 폴백
    plan = {
        "tool": "nudge_perception",
        "target_actor_id": "a_ghost",
        "observation": "무언가를 알아차렸다",
        "about_actor_id": None,
        "rationale": "x",
    }
    assert intervention_from_plan(plan, SNAP, TENSION, INCIDENTS, NAMES) is None


def test_nudge_empty_observation_returns_none():
    plan = {
        "tool": "nudge_perception",
        "target_actor_id": "a_minji",
        "observation": "   ",
        "about_actor_id": None,
        "rationale": "x",
    }
    assert intervention_from_plan(plan, SNAP, TENSION, INCIDENTS, NAMES) is None


def test_nudge_hallucinated_about_is_dropped_but_kept():
    # about이 후보 밖이면 버리되(null), 관측 자체는 유효하므로 nudge는 성립한다
    plan = {
        "tool": "nudge_perception",
        "target_actor_id": "a_minji",
        "observation": "이상한 낌새를 느꼈다",
        "about_actor_id": "a_ghost",
        "rationale": "x",
    }
    result = intervention_from_plan(plan, SNAP, TENSION, INCIDENTS, NAMES)
    assert result is not None
    assert result.payload["about_actor_id"] is None


# --- promote_actor 도구 -------------------------------------------------------


def test_valid_promote_spotlights_candidate_on_system_stream():
    plan = {
        "tool": "promote_actor",
        "target_actor_id": "a_seongho",
        "rationale": "박성호가 잠재 드라마의 중심 — 무대로 끌어올린다",
    }
    result = intervention_from_plan(plan, SNAP, TENSION, INCIDENTS, NAMES, model="qwen3:8b")
    assert result is not None
    assert result.tool == "promote_actor"
    assert result.stream == "system"  # 세계 사건이 아니라 제어 신호
    assert result.event_type == "system.director.spotlighted"
    assert result.stream_key == "spotlight"
    assert result.payload == {"target_actor_id": "a_seongho"}
    assert result.signals["selector"] == "llm"


def test_promote_target_outside_candidates_returns_none():
    # 없는 액터는 승격할 대상이 없다 — 규칙 폴백
    plan = {"tool": "promote_actor", "target_actor_id": "a_ghost", "rationale": "x"}
    assert intervention_from_plan(plan, SNAP, TENSION, INCIDENTS, NAMES) is None


def test_incident_and_nudge_stream_to_world():
    incident = intervention_from_plan(
        {"tool": "inject_incident", "incident_kind": "chance_encounter",
         "affected_actor_ids": ["a_minji"], "description": "x", "intensity": 0.5, "rationale": "x"},
        SNAP, TENSION, INCIDENTS, NAMES,
    )
    nudge = intervention_from_plan(
        {"tool": "nudge_perception", "target_actor_id": "a_minji", "observation": "x",
         "about_actor_id": None, "rationale": "x"},
        SNAP, TENSION, INCIDENTS, NAMES,
    )
    assert incident.stream == "world" and nudge.stream == "world"


# --- set_season_theme 도구 ----------------------------------------------------


def test_valid_season_carries_resolved_params_on_system_stream():
    plan = {"season_theme": "turmoil", "rationale": "세계 전체가 격동으로 접어들 때다"}
    result = season_from_plan(plan, SNAP, THEMES, model="qwen3:8b")
    assert result is not None
    assert result.tool == "set_season_theme"
    assert result.stream == "system"  # 시즌 페이싱 제어 신호
    assert result.event_type == "system.director.season_set"
    assert result.stream_key == "season"
    # 해석된 파라미터를 payload에 실어 소비자가 곧 적용한다
    assert result.payload == {
        "theme": "turmoil", "quiet_ticks_to_fire": 12, "max_interventions": 4,
    }
    assert result.signals["selector"] == "llm"


def test_season_outside_library_returns_none():
    # 라이브러리 밖 테마는 존재하지 않는다 — 폴백(테마 미변경)
    assert season_from_plan({"season_theme": "apocalypse", "rationale": "x"}, SNAP, THEMES) is None


def test_build_season_user_shows_themes_and_current():
    user = build_season_user(SNAP, THEMES, current_theme="calm")
    assert "calm" in user and "turmoil" in user  # 고를 수 있는 테마
    assert "calm" in user  # 현재 테마도 표시
    assert str(SNAP.drama_ma) in user


def test_build_plan_user_grounds_names_kinds_and_drama_tools_only():
    user = build_plan_user(SNAP, TENSION, INCIDENTS, NAMES)
    assert "김민지" in user and "박성호" in user  # 이름으로 그라운딩
    assert "chance_encounter" in user and "rumor_spread" in user  # 라이브러리 종류 제시
    assert "a_minji" in user  # 후보 id 명시 (대상 화이트리스트)
    # 드라마 도구 셋 — set_season_theme은 없다(시즌 계획으로 분리)
    assert "inject_incident" in user and "nudge_perception" in user and "promote_actor" in user
    assert "set_season_theme" not in user
    assert str(SNAP.quiet_ticks) in user
