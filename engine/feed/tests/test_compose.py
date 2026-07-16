"""FeedItem 승격 로직 검증 — 결정적 post_id + 스키마 적합 payload (ADR-014)."""

import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator
from lf_feed.compose import (
    build_arc_post_event,
    build_goal_post_event,
    build_post_event,
    derive_post_id,
    evaluate,
    evaluate_arc_transition,
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


def test_build_post_event_without_names_falls_back_to_ids():
    event = build_post_event(SAMPLE, drama=0.5, score=0.4, actor_names={})
    assert "a_aria_kim" in event.payload["title"]


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
