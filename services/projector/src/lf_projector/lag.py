"""프로젝션 lag 계측 — 발생→read 반영 지연의 구조화 로그 MVP (ADR-020 §1).

예산 "projection lag (전 프로젝터) < 2s"는 지금까지 관찰 수단이 없었다.
Prometheus 이전 단계로 로그 한 줄부터 시작한다: lag_seconds/LagAggregator는
계산과 발화 결정만 담당하는 순수 로직이고, datetime.now 호출과 logger 배선은
observe() — 전 프로젝터(pg/os/kuzu/redis) 공용 어댑터 — 한 곳에 모은다.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import fmean
from typing import Any


def lag_seconds(occurred_at: str, now: datetime) -> float:
    """봉투 occurred_at(ISO 8601)과 처리 시각 now(TZ-aware)의 차이(초).

    - occurred_at에 TZ가 없으면 UTC로 간주한다 — 봉투 스키마(date-time)가
      TZ를 요구하므로 방어적 보정일 뿐이다.
    - 시계 스큐는 음수로 드러난다 — 관찰이 목적이므로 감추지 않는다.
    """
    occurred = datetime.fromisoformat(occurred_at)
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    return (now - occurred).total_seconds()


@dataclass(frozen=True)
class LagSummary:
    """발화 한 번이 담는 요약 — 롤링 윈도 내 표본 기준."""

    count: int
    max_s: float
    avg_s: float


class LagAggregator:
    """롤링 윈도(최근 window건) lag 집계 + 발화 주기 결정.

    record()가 요약을 반환하면 어댑터가 그 시점에 로그 한 줄을 쓴다 —
    마지막 발화 이후 window건 도달 또는 interval_s 경과 중 먼저 오는 쪽.
    이벤트가 없으면 침묵한다: lag를 잴 재료가 없고, 무소식 감시는
    컨슈머 pending 모니터링의 몫이지 이 계측의 몫이 아니다.
    """

    def __init__(self, *, window: int = 100, interval_s: float = 30.0) -> None:
        self._window_size = window
        self._interval_s = interval_s
        self._window: deque[float] = deque(maxlen=window)
        self._since_emit = 0
        self._last_emit: datetime | None = None

    def record(self, lag_s: float, now: datetime) -> LagSummary | None:
        """표본 하나 기록. 발화 주기에 도달했으면 요약을 반환한다."""
        if self._last_emit is None:
            self._last_emit = now  # 첫 표본부터 interval을 센다 — 시작 즉시 발화 방지
        self._window.append(lag_s)
        self._since_emit += 1
        due = (
            self._since_emit >= self._window_size
            or (now - self._last_emit).total_seconds() >= self._interval_s
        )
        return self.flush(now) if due else None

    def flush(self, now: datetime) -> LagSummary | None:
        """윈도를 요약하고 발화 주기를 리셋한다. 빈 윈도는 None (발화할 것이 없다)."""
        if not self._window:
            return None
        summary = LagSummary(
            count=len(self._window),
            max_s=max(self._window),
            avg_s=fmean(self._window),
        )
        self._since_emit = 0
        self._last_emit = now
        return summary


def observe(aggregator: LagAggregator, envelope: dict[str, Any], log: logging.Logger) -> None:
    """처리 성공 봉투의 발생→반영 지연 기록 (ADR-020 §1) — 실패는 ack를 막지 않는다.

    전 프로젝터 공용 배선: 각자의 logger를 넘기므로 로그 라인의 logger 이름이
    어느 프로젝터의 lag인지 말해준다.
    """
    try:
        now = datetime.now(UTC)
        summary = aggregator.record(lag_seconds(envelope["occurred_at"], now), now)
    except (KeyError, TypeError, ValueError):
        # 계측은 부수 관찰이다 — occurred_at 결손·오형식이 프로젝션을 죽여선 안 된다
        log.debug("lag 계측 불가 — occurred_at=%r", envelope.get("occurred_at"))
        return
    if summary is not None:
        log.info(
            "projection_lag_seconds max=%.3f avg=%.3f count=%d",
            summary.max_s, summary.avg_s, summary.count,
        )
