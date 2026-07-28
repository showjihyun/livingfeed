"""인지 예산 — 이 인물이 한 번 생각할 때 쓸 수 있는 자원 (ADR-021 §3).

**요점은 비용 절감이 아니라 실험 변수다.** 지금까지 회상 슬롯·기억 예산·추론
빈도는 전부 전역 상수이거나 LOD 티어(ADR-011)가 정하는 값이었다. 그래서
"기억 용량이 작은 인물의 관계는 어떻게 달라지는가", "회상 슬롯을 줄이면 성격
일관성이 언제 무너지는가" 같은 질문을 **물어볼 수조차 없었다** — 조작할 변수가
없었기 때문이다.

세 자원이 ADR-021 §3 표의 세 행에 대응한다:

    recall_slots    회상 top-k        (Qdrant, ADR-008)
    memory_tokens   회상+작업 기억의 합산 예산 (ADR-009 컨텍스트 토큰의 액터별 상한)
    calls_per_tick  tick당 추론 호출 상한

memory_tokens가 '컨텍스트 토큰'의 전부가 아니라 **기억 부분**인 이유: 정체성·
세계·임무 프레임은 이 인물이 누구인지와 무엇을 하라는 지시라 줄이면 다른 실험이
된다(인물이 바뀐다). 인지 자원을 줄인다는 것은 기억을 줄인다는 뜻이어야 한다.

기본값은 LOD 티어에서 그대로 유도한다 — 오버라이드가 없으면 현행 동작과 정확히
같다. 오버라이드는 params.yaml의 데이터이며 코드에 인물이 박히지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

#: 회상 기본 슬롯 — 지금까지 _recall이 쓰던 하드코딩 값 (현행 동작 보존)
DEFAULT_RECALL_SLOTS = 3

#: 기억 예산 기본 — context.py의 BUDGET_EPISODES(600) + BUDGET_WORKING(1200)
DEFAULT_MEMORY_TOKENS = 1_800


@dataclass(frozen=True)
class CognitiveBudget:
    """한 인물이 한 tick에 쓸 수 있는 인지 자원.

    0 이하는 허용하지 않는다: 회상 0슬롯·기억 0토큰은 '자원이 적은 인물'이 아니라
    '기억이 없는 인물'이라 다른 실험이고, calls_per_tick 0은 그 인물을 세계에서
    지워버린다. 실험이 의도한 하한은 1이다.
    """

    recall_slots: int = DEFAULT_RECALL_SLOTS
    memory_tokens: int = DEFAULT_MEMORY_TOKENS
    calls_per_tick: int = 4

    def __post_init__(self) -> None:
        for name in ("recall_slots", "memory_tokens", "calls_per_tick"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name}는 1 이상이어야 한다 — 0은 인물을 지운다")

    def merged(self, override: dict[str, Any]) -> CognitiveBudget:
        """부분 오버라이드를 얹는다 — 준 값만 바뀌고 나머지는 티어 기본이 남는다."""
        fields = {
            key: int(override[key])
            for key in ("recall_slots", "memory_tokens", "calls_per_tick")
            if override.get(key) is not None
        }
        return replace(self, **fields) if fields else self

    def to_json(self) -> dict[str, int]:
        """결정 기록에 실리는 형태 — 어떤 예산으로 내린 결정인지가 남아야
        실험 결과를 예산과 이어 읽을 수 있다 (ADR-021 §2/§3)."""
        return {
            "recall_slots": self.recall_slots,
            "memory_tokens": self.memory_tokens,
            "calls_per_tick": self.calls_per_tick,
        }


class CognitiveBudgets:
    """액터 → 인지 예산 해석기.

    우선순위는 (1) 액터별 오버라이드 (2) 티어 기본 (3) 코드 기본이다.
    오버라이드가 비어 있으면 이 클래스는 현행 동작을 그대로 재현한다 —
    관측·실험 장치가 세계를 바꾸지 않는 것이 §3의 전제다 (ADR-021 §결과
    "관측이 시뮬레이션을 바꿀 위험").
    """

    def __init__(
        self,
        *,
        tiers: dict[str, dict[str, Any]] | None = None,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        base = CognitiveBudget()
        self._tiers = {
            tier: base.merged(values or {}) for tier, values in (tiers or {}).items()
        }
        self._overrides = overrides or {}
        self._base = base

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> CognitiveBudgets:
        """params.yaml의 cognition 절에서 — 수치는 코드가 아니라 데이터다."""
        section = params.get("cognition") or {}
        return cls(tiers=section.get("tiers"), overrides=section.get("overrides"))

    def for_actor(self, actor_id: str, tier: str) -> CognitiveBudget:
        budget = self._tiers.get(tier, self._base)
        override = self._overrides.get(actor_id)
        return budget.merged(override) if override else budget

    @property
    def has_overrides(self) -> bool:
        """실험 설정이 걸려 있는가 — 로그에 남겨 '왜 이 세계가 다르지'를 답한다."""
        return bool(self._overrides)
