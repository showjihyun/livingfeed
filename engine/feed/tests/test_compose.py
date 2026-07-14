"""FeedItem 승격 로직 검증 — 결정적 post_id + 스키마 적합 payload (ADR-014)."""

import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator
from lf_feed.compose import (
    build_goal_post_event,
    build_post_event,
    derive_post_id,
    evaluate,
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
    errors = list(Draft202012Validator(registry.payload_schema("feed.post.published")).iter_errors(event.payload))
    assert errors == []
    assert event.payload["visibility"] == "world"
    assert "김민지" in event.payload["title"] and "이루다" in event.payload["title"]
    assert "마침내" in event.payload["body"]
    assert GOAL_ACHIEVED["payload"]["description"] in event.payload["body"]
    assert "goal_achieved" in event.payload["tags"]
    assert event.payload["source_event_type"] == "actor.goal.achieved"
    # 사슬 승계 — 목표를 이룬 행동의 correlation을 잇는다
    assert event.correlation_id == GOAL_ACHIEVED["correlation_id"]


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
