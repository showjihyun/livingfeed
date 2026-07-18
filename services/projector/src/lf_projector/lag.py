"""프로젝션 lag 계측 — 구조화 로그 + 옵트인 Prometheus 히스토그램 (ADR-020 §1).

예산 "projection lag (전 프로젝터) < 2s"의 관찰 수단. 로그 한 줄
(projection_lag_seconds max/avg/count)이 여전히 1차 관측면이고, 수집기가 있는
환경에서는 LagMetrics(LF_METRICS_PORT 옵트인)가 p95 등 분위수를 더한다.
lag_seconds/LagAggregator는 계산과 발화 결정만 담당하는 순수 로직이고,
datetime.now 호출과 logger/지표 배선은 observe() — 전 프로젝터(pg/os/kuzu/redis)
공용 어댑터 — 한 곳에 모은다.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import fmean
from typing import Any

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram


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


#: 히스토그램 버킷 — 예산 2s(ADR-020)가 경계 버킷으로 또렷이 보이도록
#: 예산 아래를 촘촘히, 위(위반의 정도)를 성기게 놓는다
LAG_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)


@dataclass(frozen=True)
class KindMetrics:
    """한 프로젝터(kind 라벨)에 미리 결합된 지표 손잡이 — observe()가 쓴다."""

    lag: Histogram  # labels(kind=…) 자식
    events: Counter


class LagMetrics:
    """Prometheus 지표면 — 옵트인 (LF_METRICS_PORT로 지표 서버가 뜰 때만 생성).

    - projection_lag_seconds{kind}  발생→반영 지연 히스토그램 (p95의 재료)
    - projection_events_total{kind} 처리(ack) 이벤트 수
    registry 주입은 테스트 격리용 — 기본은 전역 REGISTRY (지표 서버가 노출하는 곳).
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        registry = REGISTRY if registry is None else registry
        self._lag = Histogram(
            "projection_lag_seconds",
            "발생(occurred_at)→프로젝션 반영 지연 초 (예산 <2s, ADR-020 §1)",
            labelnames=("kind",),
            buckets=LAG_BUCKETS,
            registry=registry,
        )
        self._events = Counter(
            "projection_events_total",
            "프로젝터가 처리(ack)한 이벤트 수",
            labelnames=("kind",),
            registry=registry,
        )

    def for_kind(self, kind: str) -> KindMetrics:
        """kind(pg/os/kuzu/redis) 라벨을 미리 결합한 손잡이를 만든다."""
        return KindMetrics(
            lag=self._lag.labels(kind=kind), events=self._events.labels(kind=kind)
        )


def observe(
    aggregator: LagAggregator,
    envelope: dict[str, Any],
    log: logging.Logger,
    metrics: KindMetrics | None = None,
) -> None:
    """처리 성공 봉투의 발생→반영 지연 기록 (ADR-020 §1) — 실패는 ack를 막지 않는다.

    전 프로젝터 공용 배선: 각자의 logger를 넘기므로 로그 라인의 logger 이름이
    어느 프로젝터의 lag인지 말해준다. metrics(옵트인)가 있으면 같은 지점에서
    projection_events_total을 올리고 lag를 히스토그램에도 싣는다.
    """
    if metrics is not None:
        metrics.events.inc()  # 처리 건수는 occurred_at 결손과 무관하게 센다
    try:
        now = datetime.now(UTC)
        lag_s = lag_seconds(envelope["occurred_at"], now)
    except (KeyError, TypeError, ValueError):
        # 계측은 부수 관찰이다 — occurred_at 결손·오형식이 프로젝션을 죽여선 안 된다
        log.debug("lag 계측 불가 — occurred_at=%r", envelope.get("occurred_at"))
        return
    if metrics is not None:
        metrics.lag.observe(lag_s)
    summary = aggregator.record(lag_s, now)
    if summary is not None:
        log.info(
            "projection_lag_seconds max=%.3f avg=%.3f count=%d",
            summary.max_s, summary.avg_s, summary.count,
        )
