"""feed-worthiness 순수 로직 검증 (ADR-014 §1단)."""

import pytest
from lf_feed.scoring import RarityTracker, ScoringConfig, drama_score, worthiness

CFG = ScoringConfig()


def test_drama_orders_conflict_over_routine():
    confront = drama_score("confront", has_target=True, cfg=CFG)
    speak = drama_score("speak", has_target=True, cfg=CFG)
    move = drama_score("move", has_target=False, cfg=CFG)
    assert confront > speak > move


def test_drama_target_bonus_and_clamp():
    assert drama_score("speak", has_target=True, cfg=CFG) == drama_score(
        "speak", has_target=False, cfg=CFG
    ) + CFG.target_bonus
    assert drama_score("confront", has_target=True, cfg=CFG) <= 1.0


def test_unknown_action_uses_default():
    assert drama_score("juggle", has_target=False, cfg=CFG) == CFG.default_drama


def test_worthiness_is_weighted_sum_clamped():
    assert worthiness(1.0, 1.0, 1.0, 1.0, CFG) == pytest.approx(1.0)
    assert worthiness(0.0, 0.0, 0.0, 0.0, CFG) == 0.0
    expected = CFG.w_drama * 0.5 + CFG.w_rarity * 1.0
    assert abs(worthiness(0.5, 0.0, 1.0, 0.0, CFG) - expected) < 1e-9


def test_rarity_first_observation_is_novel():
    tracker = RarityTracker(window=8)
    assert tracker.rarity("speak") == 1.0


def test_rarity_decays_with_repetition():
    tracker = RarityTracker(window=8)
    first = tracker.rarity("speak")
    tracker.observe("speak")
    tracker.observe("speak")
    later = tracker.rarity("speak")
    assert later < first
    # 다른 종류의 행동은 여전히 희소하다
    assert tracker.rarity("confront") == 1.0


def test_rarity_window_eviction():
    tracker = RarityTracker(window=2)
    tracker.observe("speak")
    tracker.observe("speak")
    assert tracker.rarity("speak") == 0.0
    tracker.observe("move")  # speak 하나 축출
    tracker.observe("move")  # 남은 speak 축출 — 창은 move 만
    assert tracker.rarity("speak") == 1.0
