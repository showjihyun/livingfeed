"""FeedItem 승격 로직 검증 — 결정적 post_id + 스키마 적합 payload (ADR-014)."""

import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator
from lf_feed.compose import (
    build_arc_post_event,
    build_debut_post_event,
    build_goal_post_event,
    build_post_event,
    derive_post_id,
    evaluate,
    evaluate_arc_transition,
    evaluate_debut,
    evaluate_goal_achievement,
)
from lf_feed.scoring import RarityTracker, ScoringConfig
from lf_schemas import registry

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

SAMPLE = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages" / "schemas" / "samples" / "actor.action.performed.001.json"
    ).read_text(encoding="utf-8")
)

NAMES = {"a_aria_kim": "김아리", "a_junho_park": "박준호"}


def test_derive_post_id_is_deterministic_valid_ulid():
    a = derive_post_id(SAMPLE["event_id"])
    b = derive_post_id(SAMPLE["event_id"])
    assert a == b
    assert ULID_RE.match(a)
    # 타임스탬프 48비트 승계 — 커서 순서가 사건 시간 순서를 따른다
    assert a[:10] == SAMPLE["event_id"][:10]
    assert a != SAMPLE["event_id"]


def test_derive_post_id_differs_per_source():
    other = SAMPLE["event_id"][:-1] + ("X" if SAMPLE["event_id"][-1] != "X" else "Z")
    assert derive_post_id(SAMPLE["event_id"]) != derive_post_id(other)


def test_build_post_event_payload_matches_schema():
    event = build_post_event(SAMPLE, drama=0.55, score=0.47, actor_names=NAMES)
    schema = registry.payload_schema("feed.post.published")
    errors = list(Draft202012Validator(schema).iter_errors(event.payload))
    assert errors == []

    assert event.stream == "feed"
    assert event.type == "feed.post.published"
    assert event.event_id == event.stream_key == derive_post_id(SAMPLE["event_id"])
    assert event.causation_id == SAMPLE["event_id"]
    assert event.correlation_id == SAMPLE["correlation_id"]
    assert event.payload["participants"] == ["a_aria_kim", "a_junho_park"]
    assert "김아리" in event.payload["title"] and "박준호" in event.payload["title"]
    assert event.payload["body"] == SAMPLE["payload"]["intent"]
    assert event.payload["tags"] == ["speak"]


def test_build_post_event_without_names_grounds_to_anonymous():
    # 이름을 모르는 액터도 원시 id는 화면 문장에 싣지 않는다 — '누군가' (내레이터의 결)
    event = build_post_event(SAMPLE, drama=0.5, score=0.4, actor_names={})
    assert "a_aria_kim" not in event.payload["title"]
    assert "누군가" in event.payload["title"]


def test_build_post_event_humanizes_raw_ids_in_body():
    """LLM intent가 옮겨 적은 식별자는 세계 어휘로 정화된다 — 실사고 재발 방지.

    실제 사례: 발행 본문에 'p_observer_0417에게 …'가 그대로 실렸다 (2026-07-18).
    """
    leaky = {
        **SAMPLE,
        "payload": {
            **SAMPLE["payload"],
            "intent": (
                "노래로 고민을 털어놓고, p_observer_0417에게 용기를 바란다. "
                "a_aria_kim의 글에 답하며."
            ),
        },
    }
    event = build_post_event(leaky, drama=0.5, score=0.4, actor_names=NAMES)
    body = event.payload["body"]
    assert "p_observer_0417" not in body and "a_aria_kim" not in body
    assert "어느 관찰자에게 용기를 바란다" in body
    assert "김아리의 글에 답하며" in body  # 액터는 실명 그라운딩


def test_llm_headline_becomes_title_with_llm_narration():
    # LLM이 지은 headline이 있으면 그것이 제목, narration_kind=llm (ADR-014 §제목 폴리시)
    withline = {**SAMPLE, "payload": {**SAMPLE["payload"], "headline": "특종을 손에 쥐다"}}
    event = build_post_event(withline, drama=0.55, score=0.47, actor_names=NAMES)
    assert event.payload["title"] == "특종을 손에 쥐다"
    assert event.payload["narration_kind"] == "llm"
    assert event.payload["body"] == SAMPLE["payload"]["intent"]  # 본문은 여전히 intent (중복 없음)
    schema = registry.payload_schema("feed.post.published")
    assert list(Draft202012Validator(schema).iter_errors(event.payload)) == []


def test_confess_and_sever_title_templates():
    """고백·절교 제목 템플릿 — 대상 유무에 따라 갈라진다 (ADR-016 stage 전이 행동)."""
    cases = {
        "confess": ("김아리, 박준호에게 마음을 고백하다", "김아리, 오래 품은 마음을 꺼내다"),
        "sever": ("김아리, 박준호와의 관계를 끊다", "김아리, 관계 하나를 정리하다"),
    }
    for kind, (with_target, without_target) in cases.items():
        targeted = {**SAMPLE, "payload": {**SAMPLE["payload"], "action_kind": kind}}
        assert build_post_event(
            targeted, drama=0.9, score=0.6, actor_names=NAMES
        ).payload["title"] == with_target
        solo = {**targeted, "payload": {**targeted["payload"], "target_actor_id": None}}
        assert build_post_event(
            solo, drama=0.9, score=0.6, actor_names=NAMES
        ).payload["title"] == without_target


def test_missing_or_blank_headline_falls_back_to_template():
    event = build_post_event(SAMPLE, drama=0.5, score=0.4, actor_names=NAMES)
    assert event.payload["narration_kind"] == "template"
    assert "김아리" in event.payload["title"]  # action_kind 템플릿
    # 공백뿐인 headline도 템플릿 폴백 (빈 제목 방지)
    blank = {**SAMPLE, "payload": {**SAMPLE["payload"], "headline": "   "}}
    assert build_post_event(blank, drama=0.5, score=0.4, actor_names=NAMES).payload[
        "narration_kind"
    ] == "template"


GOAL_ACHIEVED = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages" / "schemas" / "samples" / "actor.goal.achieved.001.json"
    ).read_text(encoding="utf-8")
)


def test_goal_achievement_always_promotes_as_world_news():
    cfg = ScoringConfig()
    drama, score = evaluate_goal_achievement(GOAL_ACHIEVED, cfg)
    assert score >= cfg.threshold  # 완주는 언제나 피드감이다 (희소 + 부스트)
    event = build_goal_post_event(
        GOAL_ACHIEVED, drama=drama, score=score, actor_names={"a_minji_kim": "김민지"}
    )
    # 스키마 적합 + 내레이션
    schema = registry.payload_schema("feed.post.published")
    errors = list(Draft202012Validator(schema).iter_errors(event.payload))
    assert errors == []
    assert event.payload["visibility"] == "world"
    assert "김민지" in event.payload["title"] and "이루다" in event.payload["title"]
    assert "마침내" in event.payload["body"]
    assert GOAL_ACHIEVED["payload"]["description"] in event.payload["body"]
    assert "goal_achieved" in event.payload["tags"]
    assert event.payload["source_event_type"] == "actor.goal.achieved"
    # 사슬 승계 — 목표를 이룬 행동의 correlation을 잇는다
    assert event.correlation_id == GOAL_ACHIEVED["correlation_id"]


ARC_PLANNED = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages" / "schemas" / "samples" / "system.director.arc_planned.001.json"
    ).read_text(encoding="utf-8")
)


def test_arc_transition_promotes_as_world_news():
    cfg = ScoringConfig()
    drama, score = evaluate_arc_transition(cfg)
    assert score >= cfg.threshold  # 장 전환은 언제나 피드감이다 (희소 + 부스트)
    event = build_arc_post_event(
        ARC_PLANNED, previous_stage="newcomer", drama=drama, score=score,
        actor_names={"a_minji_kim": "김민지"},
    )
    schema = registry.payload_schema("feed.post.published")
    assert list(Draft202012Validator(schema).iter_errors(event.payload)) == []
    assert event.payload["visibility"] == "world"
    assert event.payload["title"] == "김민지, 인생의 장이 넘어가다"
    # 이전 장과 새 장이 한글 라벨로 — intention이 서사의 방향을 준다
    assert "사회 초년기" in event.payload["body"] and "정착·방황기" in event.payload["body"]
    assert ARC_PLANNED["payload"]["intention"] in event.payload["body"]
    assert event.payload["tags"] == ["arc_transition", "settling"]
    assert event.payload["source_event_type"] == "system.director.arc_planned"
    assert event.actor_id == "a_minji_kim"  # 장이 넘어간 건 그 인물의 삶이다
    assert event.correlation_id == ARC_PLANNED["correlation_id"]  # Director 사슬 승계


def test_first_arc_opens_the_story():
    drama, score = evaluate_arc_transition(ScoringConfig())
    event = build_arc_post_event(
        ARC_PLANNED, previous_stage=None, drama=drama, score=score,
        actor_names={"a_minji_kim": "김민지"},
    )
    assert "이야기의 첫 장이 열리다" in event.payload["title"]
    assert "정착·방황기" in event.payload["body"]


IDENTITY = {
    "event_id": "01JZK7Q3W0000000000000000D",
    "world_id": "w_main",
    "stream": "actor",
    "type": "actor.identity.declared",
    "schema_version": 1,
    "actor_id": "a_saebyeok_jung",
    "tick": 300,
    "occurred_at": "2026-07-17T12:00:00Z",
    "causation_id": None,
    "correlation_id": "01JZK7Q3W0000000000000000D",
    "payload": {
        "name": "정새벽",
        "archetype": "quiet_dreamer",
        "bio": "28세 새벽 배송 기사. 남들이 잠든 시간에 도시를 돌며 소설의 문장을 줍는다.",
        "goals": [{"description": "첫 단편을 완성한다", "priority": 0.8}],
        "created_by": "p_observer_0417",
    },
}


def test_debut_post_marks_worlds_newest_resident():
    """정체성 선언 → 데뷔 포스트 — 방생은 세계의 사건이다 (페르소나 스튜디오).

    세계당 1회 선언이라 도배가 없고, created_by가 승계돼 '당신이 빚은 인물'
    저자성 표식의 원천이 된다 (plan/03 저자성).
    """
    cfg = ScoringConfig()
    drama, score = evaluate_debut(cfg)
    assert score >= cfg.threshold  # 데뷔는 언제나 피드에 닿는다

    event = build_debut_post_event(IDENTITY, drama=drama, score=score)
    schema = registry.payload_schema("feed.post.published")
    assert list(Draft202012Validator(schema).iter_errors(event.payload)) == []
    assert event.payload["title"] == "정새벽, 세계에 첫발을 딛다"
    assert event.payload["visibility"] == "world"
    assert IDENTITY["payload"]["bio"] in event.payload["body"]
    assert event.payload["tags"] == ["debut", "quiet_dreamer"]
    assert event.payload["created_by"] == "p_observer_0417"
    assert event.actor_id == "a_saebyeok_jung"
    assert event.causation_id == IDENTITY["event_id"]
    assert event.event_id == event.stream_key == derive_post_id(IDENTITY["event_id"])


def test_debut_post_without_creator_is_system_born():
    # 시스템 태생(기존 8·16인)의 선언에는 created_by가 없다 — null로 정직하게
    payload = {k: v for k, v in IDENTITY["payload"].items() if k != "created_by"}
    event = build_debut_post_event(
        {**IDENTITY, "payload": payload}, drama=0.7, score=0.5
    )
    assert event.payload["created_by"] is None


def test_evaluate_passes_interpersonal_speak_but_filters_spam():
    cfg = ScoringConfig()
    tracker = RarityTracker(cfg.rarity_window)

    _, first_score = evaluate(SAMPLE, tracker, cfg)
    assert first_score >= cfg.threshold  # 첫 대인 발화는 피드감이다

    # 같은 행동 도배 — 희소성 감쇠로 임계 밑으로 떨어져야 한다 (다양성 보정, ADR-014)
    last_score = first_score
    for _ in range(50):
        _, last_score = evaluate(SAMPLE, tracker, cfg)
    assert last_score < first_score
    assert last_score < cfg.threshold


def test_repeat_tracker_sinks_consecutive_same_resolve():
    """연속 반복 감쇠 — 같은 인물의 같은 종류 '결심'이 연달아 피드를 채우지 못한다.

    confront(0.85)는 희소성 항 없이도 임계를 넘어(0.5×0.85=0.425) 인물별
    희소성이 못 막는다 — 라이브 관측: 같은 결심 포스트 10여 건 도배
    (2026-07-17). 반복은 새 마디가 아니다: 연속 승격에 penalty^streak.
    """
    from lf_feed.scoring import RepeatTracker

    cfg = ScoringConfig()
    tracker = RepeatTracker(
        penalty=cfg.repeat_penalty, window_ticks=cfg.repeat_window_ticks
    )
    author, kind = "a_resolver", "confront"
    base = 0.625  # confront 첫 관측(드라마 0.85 + 희소성 1.0)의 실제 점수대

    # 첫 결심은 온전히 — 마디다
    assert tracker.penalty(author, kind, tick=100) == 1.0
    tracker.observe(author, kind, tick=100, promoted=True)

    # 연속 같은 결심 — 임계 아래로 가라앉는다
    assert base * tracker.penalty(author, kind, tick=102) < cfg.threshold
    tracker.observe(author, kind, tick=102, promoted=False)
    # 거절은 streak을 늘리지도 풀지도 않는다 — 창이 닫혀 있는 동안 계속 잠잠
    assert base * tracker.penalty(author, kind, tick=104) < cfg.threshold

    # 다른 종류의 행동이 승격되면 흐름이 바뀐 것 — 반복이 아니다
    assert tracker.penalty(author, "speak", tick=105) == 1.0
    tracker.observe(author, "speak", tick=105, promoted=True)
    assert tracker.penalty(author, kind, tick=106) == 1.0

    # 창(repeat_window_ticks)이 지나면 같은 종류도 새 마디다
    tracker.observe(author, kind, tick=106, promoted=True)
    assert tracker.penalty(author, kind, tick=106 + cfg.repeat_window_ticks) == 1.0

    # 다른 인물은 서로 독립
    assert tracker.penalty("a_someone_else", kind, tick=102) == 1.0


def test_rarity_is_per_actor_not_global():
    """희소성은 인물별이다 — 한 사람의 도배를 막되, 모두의 근황을 막지 않는다.

    SNS 리듬(16명이 각자 근황 speak)에서 kind 전역 희소성은 서로의 글을
    깎아 두 번째 speak부터 아무도 임계를 못 넘게 만든다 (2026-07-17 관측).
    """
    cfg = ScoringConfig()
    tracker = RarityTracker(cfg.rarity_window)

    _, first = evaluate(SAMPLE, tracker, cfg)
    assert first >= cfg.threshold

    # 다른 인물의 같은 종류(speak) 근황 — 앞사람의 발화가 내 희소성을 깎지 않는다
    other = {**SAMPLE, "actor_id": "a_someone_else"}
    _, second = evaluate(other, tracker, cfg)
    assert second >= cfg.threshold
    assert second == first  # 서로 독립 — 같은 조건이면 같은 점수
