"""서사 신호 계산 — 순수 규칙/통계, LLM 없음 (ADR-013 §관찰).

이벤트 스트림을 tick 단위 drama 기여도로 접어 이동평균과 침체를 감지한다.
결정적: 같은 이벤트 열 → 같은 신호 (리플레이 재현).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Any

import yaml


@cache
def default_params() -> dict[str, Any]:
    """params.yaml — Drama Score 구성요소의 단일 원천 (ADR-013 완화책)."""
    text = (files("lf_director") / "params.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def drama_contribution(envelope: dict[str, Any], params: dict[str, Any] | None = None) -> float:
    """이벤트 하나의 drama 기여도 — 구성요소는 전부 설정값이다."""
    params = params or default_params()
    rules = params["drama_contribution"]
    kind = envelope["type"]
    payload = envelope["payload"]

    if kind == "actor.action.performed":
        rule = rules[kind]
        base = rule["base_by_kind"].get(payload["action_kind"], rule["default"])
        if payload.get("target_actor_id"):
            base *= rule["target_multiplier"]
        return base
    if kind == "actor.emotion.shifted":
        top = max((e["intensity"] for e in payload.get("emotions", [])), default=0.0)
        return top * rules[kind]["intensity_weight"]
    if kind == "feed.post.published":
        return payload["drama_score"] * rules[kind]["drama_weight"]
    return 0.0


@dataclass(frozen=True)
class Snapshot:
    tick: int
    drama_ma: float      # 창 내 tick당 평균 drama
    quiet_ticks: int     # 임계 미만 연속 tick 수


class DramaWindow:
    """tick별 drama 합의 롤링 창 — 침체 감지의 상태 (ADR-013 §침체 감지)."""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self._params = params or default_params()
        obs = self._params["observation"]
        self._window: deque[float] = deque(maxlen=int(obs["window_ticks"]))
        self._threshold = float(obs["quiet_threshold"])
        self._quiet_ticks = 0
        self._current_tick: int | None = None
        self._current_sum = 0.0

    def observe(self, envelope: dict[str, Any]) -> None:
        """이벤트를 현재 tick 버킷에 누적한다 (tick 경계는 close_tick이 만든다)."""
        self._current_sum += drama_contribution(envelope, self._params)

    def close_tick(self, tick: int) -> Snapshot:
        """tick 종료 — 버킷을 창에 넣고 신호 스냅샷을 돌려준다."""
        self._window.append(self._current_sum)
        self._current_sum = 0.0
        self._current_tick = tick

        drama_ma = sum(self._window) / len(self._window)
        if drama_ma < self._threshold:
            self._quiet_ticks += 1
        else:
            self._quiet_ticks = 0
        return Snapshot(tick=tick, drama_ma=round(drama_ma, 4), quiet_ticks=self._quiet_ticks)

    def reset_quiet(self) -> None:
        """개입 직후 침체 카운터 리셋 — 같은 신호로 연발 개입하지 않는다."""
        self._quiet_ticks = 0
