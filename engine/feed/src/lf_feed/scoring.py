"""feed-worthiness 채점 — 편집 1단의 순수 로직 (ADR-014 §1단).

worthiness = w_drama·drama + w_proximity·관계근접도 + w_rarity·희소성 + w_boost·Director boost

MVP의 정직한 축소: 관계 근접도는 Relationship Engine(ADR-016) 부재로 0,
Director boost는 Director AI(ADR-013) 부재로 0 — 가중치 구조는 유지한다.
임계 자동 조정(PID식 발행률 피드백, ADR-014 완화책)은 발행률 관측이
생기는 운영 단계의 후속이다.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field


def _default_action_drama() -> dict[str, float]:
    """action_kind별 기본 드라마 점수 — 대인 갈등 > 협력 > 일상 (docs/plan/06).

    confess/sever는 관계의 이름이 바뀌는 stage 전이 행동 (ADR-016) —
    갈등(confront)보다 위, 드라마의 정점이다.
    """
    return {
        "confess": 0.90,
        "sever": 0.90,
        "confront": 0.85,
        "help": 0.60,
        "speak": 0.45,
        "work": 0.25,
        "move": 0.15,
        "rest": 0.10,
    }


@dataclass(frozen=True)
class ScoringConfig:
    """편집 계수 — 전부 설정값이다 (ADR-014: 참여 극대화 단일 목표 금지)."""

    action_drama: dict[str, float] = field(default_factory=_default_action_drama)
    default_drama: float = 0.30
    #: 대상 액터가 있는 행동은 대인 사건 — 드라마 가산
    target_bonus: float = 0.10
    w_drama: float = 0.5
    w_proximity: float = 0.2
    w_rarity: float = 0.2
    w_boost: float = 0.1
    #: 승격 임계 — 너무 높으면 빈 피드, 낮으면 노이즈 (ADR-014 리스크)
    threshold: float = 0.35
    #: 희소성 관측 창 (최근 행동 수)
    rarity_window: int = 256


def drama_score(action_kind: str, *, has_target: bool, cfg: ScoringConfig) -> float:
    base = cfg.action_drama.get(action_kind, cfg.default_drama)
    if has_target:
        base += cfg.target_bonus
    return min(1.0, max(0.0, base))


def worthiness(
    drama: float, proximity: float, rarity: float, boost: float, cfg: ScoringConfig
) -> float:
    score = (
        cfg.w_drama * drama
        + cfg.w_proximity * proximity
        + cfg.w_rarity * rarity
        + cfg.w_boost * boost
    )
    return min(1.0, max(0.0, score))


class RarityTracker:
    """최근 행동 창에서 action_kind의 희소성을 추정한다 (같은 행동 도배 감쇠).

    프로세스-로컬 근사다 — 재시작 시 초기화되며, 중복 승격은 결정적
    post_id(compose.derive_post_id)가 흡수하므로 정확성에 영향이 없다.
    """

    def __init__(self, window: int) -> None:
        self._recent: deque[str] = deque(maxlen=window)
        self._counts: Counter[str] = Counter()

    def rarity(self, action_kind: str) -> float:
        """0(창을 가득 채운 흔한 행동)..1(첫 관측). 관측 전에 조회한다."""
        if not self._recent:
            return 1.0
        return 1.0 - (self._counts[action_kind] / len(self._recent))

    def observe(self, action_kind: str) -> None:
        if len(self._recent) == self._recent.maxlen:
            evicted = self._recent[0]
            self._counts[evicted] -= 1
            if self._counts[evicted] <= 0:
                del self._counts[evicted]
        self._recent.append(action_kind)
        self._counts[action_kind] += 1
