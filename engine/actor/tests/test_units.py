"""페르소나·Context Fabric·규칙 폴백의 순수 로직 검증 (ADR-009/012)."""

from datetime import UTC, datetime

from jsonschema import Draft202012Validator
from lf_actor.arc import Arc
from lf_actor.context import WorldContext, build
from lf_actor.persona import load_persona, load_personas
from lf_actor.phases import lod_after_perception, sanitize_target
from lf_actor.rules import fallback_action, routine_action
from lf_schemas import registry
from lf_tick.lod import ActorLod, Tier

from .conftest import PERSONAS_DIR

ACTION_SCHEMA = registry.payload_schema("actor.action.performed")
WORLD = WorldContext(world_id="w_test", tick=7, world_time=datetime(2026, 3, 1, 12, tzinfo=UTC))


def test_load_aria_persona():
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    assert aria.id == "a_aria_kim"
    assert aria.name == "김아리"
    assert "탐사보도" in aria.identity_core
    assert aria.needs_bias["achievement"] == 0.90
    assert aria.goals[0]["id"] == "g_expose_corruption"


def test_load_personas_directory():
    personas = load_personas(PERSONAS_DIR)
    assert any(p.id == "a_aria_kim" for p in personas)


def test_context_bundle_is_deterministic_and_ordered():
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    working = ["tick 6: 나는 work — 취재 노트를 정리했다"]

    a = build(aria, working, WORLD, trace_id="t-fixed")
    b = build(aria, working, WORLD, trace_id="t-fixed")
    assert a == b  # 순수 함수 (ADR-009 규칙 5)

    # system = 정적 정체성 프리픽스 (캐시 대상), user = 변동 섹션 고정 순서
    assert "김아리" in a.system
    assert a.user.index("## 작업 기억") < a.user.index("## 세계 상황") < a.user.index("## 임무")
    assert "취재 노트" in a.user


def test_context_arc_section_frames_decision_first():
    # 인생 아크(있으면)가 최상위 프레임 — 다른 어떤 변동 섹션보다 앞에 온다 (ADR-013)
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    arc = Arc(stage="newcomer", intention="이 도시에서 자기 자리를 만들기 시작한다")
    bundle = build(aria, ["tick 6: 나는 work — 취재"], WORLD, trace_id="t", arc=arc)
    assert bundle.user.index("## 인생 아크") < bundle.user.index("## 떠오르는 기억")
    assert "사회 초년기" in bundle.user  # stage 코드가 아니라 한글 라벨로 말한다
    assert "자기 자리를 만들기 시작한다" in bundle.user


def test_context_without_arc_omits_section():
    # 아직 아크를 받지 않은 액터는 그저 일상을 산다 — 섹션 자체가 없다
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    bundle = build(aria, [], WORLD, trace_id="t")
    assert "## 인생 아크" not in bundle.user


def test_context_relationships_between_arc_and_episodes():
    # Relationship(3)은 아크 뒤·에피소드 앞 — ADR-009 고정 순서 (Relationship(3) < Episodes(4))
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    arc = Arc(stage="newcomer", intention="이 도시에서 자기 자리를 만들기 시작한다")
    bundle = build(
        aria, [], WORLD, trace_id="t", arc=arc,
        relationships="- 박준호: 앙금이 남아 있다 — 쌓인 것이 쉽게 사라지지 않는다",
    )
    assert (
        bundle.user.index("## 인생 아크")
        < bundle.user.index("## 얽힌 사람들")
        < bundle.user.index("## 떠오르는 기억")
    )
    assert "박준호" in bundle.user


def test_context_without_relationships_omits_section():
    # 아직 아무와도 얽히지 않은 액터 — 섹션 자체가 없다 (아크와 같은 규약)
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    bundle = build(aria, [], WORLD, trace_id="t")
    assert "## 얽힌 사람들" not in bundle.user


def test_context_unknown_stage_falls_back_to_code():
    # 닫힌 어휘 밖 단계(미래 확장)라도 컨텍스트 조립은 깨지지 않는다
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    bundle = build(aria, [], WORLD, trace_id="t", arc=Arc(stage="wanderer", intention="떠돈다"))
    assert "wanderer에 있다" in bundle.user


def test_context_working_memory_budget_truncates_oldest():
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    # 예산(1200 tokens ≈ 3000 chars)을 초과하는 항목들 — 최신 우선으로 담고 나머지 절단
    entries = [f"tick {i}: " + ("긴 기억 " * 60) for i in range(100, 0, -1)]
    bundle = build(aria, entries, WORLD, trace_id="t")
    assert "tick 100" in bundle.user  # 최신은 유지
    assert "tick 1:" not in bundle.user  # 오래된 것은 절단 (ADR-009 규칙 2)


def test_fallback_action_is_valid_and_personalized():
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    action = fallback_action(aria, tick=42, trace_id="t-1")
    assert not list(Draft202012Validator(ACTION_SCHEMA).iter_errors(action))
    assert action["action_kind"] == "work"  # achievement가 최강 욕구
    assert action["decision_trace"]["tier"] == "cold_rule"
    assert action == fallback_action(aria, tick=42, trace_id="t-1")  # 결정적


def test_routine_action_carries_arc_intention():
    # 아크 있는 잠든 액터 — 일과 서술에 인생 방향이 스민다 (ADR-013/plan-08)
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    arc = Arc(stage="prime", intention="정점에서 다음 이유를 찾는다")
    action = routine_action(aria, tick=100, trace_id="t-1", arc=arc)
    assert not list(Draft202012Validator(ACTION_SCHEMA).iter_errors(action))
    assert "정점에서 다음 이유를 찾는다" in action["intent"]
    # 아크 없으면 일과는 그대로다 (기존 서술 불변)
    plain = routine_action(aria, tick=100, trace_id="t-1")
    assert "정점에서" not in plain["intent"]


def test_routine_action_narrates_period_by_needs():
    """Cold 일과 — 최강 욕구가 기조, 두 번째 욕구가 양념 (ADR-012 '일과 이벤트 생성')."""
    aria = load_persona(PERSONAS_DIR / "aria-kim.yaml")
    action = routine_action(aria, tick=100, trace_id="t-1")
    assert not list(Draft202012Validator(ACTION_SCHEMA).iter_errors(action))
    assert action["action_kind"] == "work"  # achievement가 최강 욕구 — 일과의 기조
    assert action["params"] == {"fallback": True, "routine": True}
    assert action["decision_trace"]["tier"] == "cold_rule"
    assert "틈틈이" in action["intent"]  # 두 번째 욕구가 스몄다
    assert action == routine_action(aria, tick=100, trace_id="t-1")  # 결정적
    # 순간 행동(fallback)과 다른 결 — 기간의 서술이다
    assert action["intent"] != fallback_action(aria, 100, "t-1")["intent"]


VALID_IDS = {"a_aria_kim", "a_junho_park"}


def test_sanitize_target_keeps_real_actor():
    payload = {"action_kind": "confront", "target_actor_id": "a_junho_park"}
    out = sanitize_target(payload, VALID_IDS, "a_aria_kim")
    assert out["target_actor_id"] == "a_junho_park"  # 유효 대상은 유지


def test_sanitize_target_nulls_hallucinated_id():
    # LLM이 지어낸 없는 대상 — 이벤트로 굳기 전에 끊는다 (피드·관계·그래프로 번짐 방지)
    payload = {"action_kind": "speak", "target_actor_id": "a_grandson_kang", "intent": "x"}
    out = sanitize_target(payload, VALID_IDS, "a_aria_kim")
    assert out["target_actor_id"] is None
    assert payload["target_actor_id"] == "a_grandson_kang"  # 입력 불변(새 dict 반환)
    assert out["intent"] == "x"  # 나머지 필드는 보존


def test_sanitize_target_nulls_self_target():
    payload = {"action_kind": "help", "target_actor_id": "a_aria_kim"}
    out = sanitize_target(payload, VALID_IDS, "a_aria_kim")
    assert out["target_actor_id"] is None  # 자기 자신은 대상이 아니다


def test_sanitize_target_passes_none_through_unchanged():
    payload = {"action_kind": "work", "target_actor_id": None}
    out = sanitize_target(payload, VALID_IDS, "a_aria_kim")
    assert out is payload  # 손댈 것 없으면 그대로 (불필요한 복사 없음)


def _envelope(event_type: str, payload: dict | None = None) -> dict:
    return {"type": event_type, "payload": payload or {}}


def test_lod_reply_obligation_promotes_to_hot():
    # dm/comment는 즉시 응답 대상 → 어느 티어에서든 Hot 승격 (상호작용 우선)
    warm = ActorLod(tier=Tier.WARM, last_interest_tick=0)
    out = lod_after_perception(warm, [_envelope("player.dm.sent")], tick=42)
    assert out.tier is Tier.HOT
    assert out.last_interest_tick == 42
    # 다른 신호와 섞여도 응답 의무가 있으면 승격
    mixed = lod_after_perception(
        warm,
        [_envelope("world.incident.occurred", {"intensity": 0.3}),
         _envelope("player.comment.posted")],
        42,
    )
    assert mixed.tier is Tier.HOT


def test_lod_director_nudge_promotes_to_hot():
    # Director의 사적 지목은 반응을 기대하고 심은 지각 — 잠든 채 두면 다음 due까지
    # 썩는다. 즉시 Hot으로 깨운다 (ADR-013)
    cold = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    out = lod_after_perception(cold, [_envelope("world.observation.surfaced")], tick=42)
    assert out.tier is Tier.HOT
    assert out.last_interest_tick == 42


def test_lod_high_intensity_incident_promotes_low_touches():
    # 고강도 사건은 잠든 액터도 깨운다 — 저강도는 관심 신호일 뿐 (ADR-011 §관심 신호)
    cold = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    hot = lod_after_perception(
        cold, [_envelope("world.incident.occurred", {"intensity": 0.8})], tick=42
    )
    assert hot.tier is Tier.HOT
    mild = lod_after_perception(
        cold, [_envelope("world.incident.occurred", {"intensity": 0.5})], tick=42
    )
    assert mild.tier is Tier.COLD and mild.last_interest_tick == 42


def test_lod_high_intensity_threshold_is_tunable():
    # 고강도 승격 임계는 운영 노브다 (LF_PROMOTE_INTENSITY) — 기본 0.7
    cold = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    incident = [_envelope("world.incident.occurred", {"intensity": 0.5})]
    assert lod_after_perception(cold, incident, 42).tier is Tier.COLD
    assert lod_after_perception(cold, incident, 42, high_intensity=0.4).tier is Tier.HOT


def test_lod_soft_signal_touches_without_promoting():
    # 반응·저강도 사건은 관심 신호 — 티어 유지, 강등 타이머만 리셋
    warm = ActorLod(tier=Tier.WARM, last_interest_tick=0)
    out = lod_after_perception(
        warm, [_envelope("world.incident.occurred", {"intensity": 0.4})], tick=42
    )
    assert out.tier is Tier.WARM  # 승격 안 함
    assert out.last_interest_tick == 42  # 하지만 관심은 갱신(강등 지연)
    cold = lod_after_perception(
        ActorLod(tier=Tier.COLD, last_interest_tick=0),
        [_envelope("player.reaction.added")], 42,
    )
    assert cold.tier is Tier.COLD and cold.last_interest_tick == 42
