"""개입 결정 규칙 — MVP는 도구 선택도 규칙 기반 (ADR-013 §실행 모델 '단계적 도입').

hard rule은 여기서 집행된다:
- 예산: 창(세계 1시간)당 개입 수 상한
- 같은 correlation 사슬 연속 개입 금지
- 도구 화이트리스트 밖의 개입은 존재하지 않는다 (반환 타입이 곧 화이트리스트)
LLM 개입 선택(임계 초과 시 상위 모델)은 도파민 루프 검증 후의 후속이다.
"""

from __future__ import annotations

import zlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from lf_director.signals import Snapshot, default_params


@dataclass(frozen=True)
class Intervention:
    """규칙이 고른 개입 — MVP 도구는 inject_incident뿐이다."""

    tool: str
    incident_kind: str
    description: str
    location_id: str | None
    affected_actor_ids: list[str]
    intensity: float
    reason: str
    signals: dict[str, Any]


@dataclass
class BudgetState:
    """개입 예산의 상태 — system.director.intervened 재소비로 복원된다 (ADR-013)."""

    recent_ticks: deque[int] = field(default_factory=deque)
    last_correlation_id: str | None = None
    interventions_total: int = 0

    def record(self, tick: int, correlation_id: str | None) -> None:
        self.recent_ticks.append(tick)
        self.last_correlation_id = correlation_id
        self.interventions_total += 1

    def remaining(self, tick: int, params: dict[str, Any]) -> int:
        budget = params["budget"]
        window = int(budget["window_ticks"])
        while self.recent_ticks and self.recent_ticks[0] <= tick - window:
            self.recent_ticks.popleft()
        return max(0, int(budget["max_interventions"]) - len(self.recent_ticks))


def is_fireable(
    snapshot: Snapshot, budget: BudgetState, params: dict[str, Any] | None = None
) -> bool:
    """개입 발화 조건 — 침체 임계 초과 + 예산 잔여 (hard rule 게이트, ADR-013).

    규칙 경로(decide)와 LLM 경로(planner)가 공유한다 — LLM 호출은 반드시 이
    게이트를 통과한 뒤에만 일어난다. 게이트 자체는 결정적 통계다.
    """
    params = params or default_params()
    obs = params["observation"]
    if snapshot.quiet_ticks < int(obs["quiet_ticks_to_fire"]):
        return False
    return budget.remaining(snapshot.tick, params) > 0


def decide(
    snapshot: Snapshot,
    budget: BudgetState,
    tension_pairs: list[list[Any]],
    *,
    params: dict[str, Any] | None = None,
) -> Intervention | None:
    """신호가 임계를 넘고 예산이 남았을 때만 개입한다. 아니면 None — 세계는 자율이다."""
    params = params or default_params()
    obs = params["observation"]
    if not is_fireable(snapshot, budget, params):
        return None

    # 같은 도구 반복의 기계감 방지 — 누적 개입 수 기준 결정적 순환 (ADR-013 완화책)
    incidents = params["incidents"]
    incident = incidents[
        zlib.crc32(f"incident:{budget.interventions_total}".encode()) % len(incidents)
    ]

    # 갈등 후보(그래프 tension 질의)가 있으면 그 쌍을 사건의 영향권에 놓는다 —
    # 조종이 아니라 무대 배치다: 반응은 액터의 몫 (ADR-013 간접 개입)
    affected = [tension_pairs[0][0], tension_pairs[0][1]] if tension_pairs else []

    return Intervention(
        tool="inject_incident",
        incident_kind=incident["kind"],
        description=incident["description"],
        location_id=incident.get("location_id"),
        affected_actor_ids=affected,
        intensity=float(incident["intensity"]),
        reason=(
            f"침체 감지 — drama 이동평균 {snapshot.drama_ma}이 "
            f"{snapshot.quiet_ticks} tick 지속 (임계 {obs['quiet_threshold']}/"
            f"{obs['quiet_ticks_to_fire']})"
        )[:300],
        signals={
            "drama_ma": snapshot.drama_ma,
            "quiet_ticks": snapshot.quiet_ticks,
            "tension_top": tension_pairs[:3],
            "selector": "rule",  # 개입 선택 주체 — 감사에서 규칙/LLM 구분 (ADR-013)
        },
    )
