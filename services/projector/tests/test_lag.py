"""프로젝션 lag 계측 검증 (ADR-020 §1) — 순수 로직 경계 + 전 프로젝터 배선 스모크.

경계의 중심: TZ 정규화(Z/오프셋/결손), 빈 집계의 침묵, 발화 주기(건수·시간)의 리셋.
배선 스모크는 프로젝터마다 하나: 성공 경로가 observe를 지나 ack에 닿는가.
Prometheus 지표면(옵트인)은 격리 CollectorRegistry로 검증한다 — 히스토그램 축적,
예산 버킷 경계, 포트 미설정 시 서버 미기동.
"""

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from lf_projector import main as projector_main
from lf_projector.config import Config
from lf_projector.kuzu_projector import KuzuProjector
from lf_projector.lag import (
    LAG_BUCKETS,
    KindMetrics,
    LagAggregator,
    LagMetrics,
    lag_seconds,
    observe,
)
from lf_projector.os_projector import OsProjector
from lf_projector.pg_projector import PgProjector
from lf_projector.redis_projector import RedisProjector
from prometheus_client import CollectorRegistry

from .conftest import sample

T0 = datetime(2026, 7, 11, 9, 2, 20, tzinfo=UTC)


# ── lag_seconds — TZ 경계 ──────────────────────────────────────────────


def test_lag_seconds_with_z_suffix():
    assert lag_seconds("2026-07-11T09:02:20Z", T0 + timedelta(seconds=1.5)) == 1.5


def test_lag_seconds_normalizes_offset_timezone():
    # +09:00 표기라도 같은 순간이면 같은 lag — 오프셋이 값에 새어들면 안 된다
    assert lag_seconds("2026-07-11T18:02:20+09:00", T0 + timedelta(seconds=2)) == 2.0


def test_lag_seconds_assumes_utc_when_naive():
    # 스키마 위반(TZ 결손)은 UTC로 보정 — 계측이 프로젝션을 죽여선 안 된다
    assert lag_seconds("2026-07-11T09:02:20", T0 + timedelta(seconds=3)) == 3.0


def test_lag_seconds_keeps_clock_skew_negative():
    # 시계 스큐는 감추지 않는다 — 음수가 곧 스큐의 관찰이다
    assert lag_seconds("2026-07-11T09:02:20Z", T0 - timedelta(seconds=1)) == -1.0


def test_lag_seconds_rejects_garbage():
    with pytest.raises(ValueError):
        lag_seconds("어제쯤", T0)


# ── LagAggregator — 발화 결정 ──────────────────────────────────────────


def test_empty_aggregator_stays_silent():
    assert LagAggregator().flush(T0) is None  # 빈 윈도 — 발화할 것이 없다


def test_emits_every_window_count():
    agg = LagAggregator(window=3, interval_s=999)
    assert agg.record(1.0, T0) is None
    assert agg.record(2.0, T0) is None
    summary = agg.record(3.0, T0)
    assert summary is not None
    assert (summary.count, summary.max_s, summary.avg_s) == (3, 3.0, 2.0)
    # 발화가 건수 카운터를 리셋한다 — 곧바로 다시 발화하지 않는다
    assert agg.record(4.0, T0) is None


def test_emits_after_interval_elapsed():
    agg = LagAggregator(window=100, interval_s=30)
    assert agg.record(1.0, T0) is None  # 첫 표본은 발화하지 않고 interval 기점이 된다
    assert agg.record(1.0, T0 + timedelta(seconds=29)) is None
    summary = agg.record(4.0, T0 + timedelta(seconds=31))
    assert summary is not None
    assert (summary.count, summary.max_s) == (3, 4.0)
    # 발화가 시간 기점도 리셋한다
    assert agg.record(1.0, T0 + timedelta(seconds=32)) is None


def test_window_is_rolling():
    agg = LagAggregator(window=2, interval_s=999)
    agg.record(10.0, T0)
    agg.record(1.0, T0)  # 2건째 — 발화 (10.0, 1.0)
    agg.record(1.0, T0)
    summary = agg.record(1.0, T0)  # 다시 2건째 — 10.0은 윈도 밖으로 밀려났다
    assert summary is not None
    assert (summary.count, summary.max_s, summary.avg_s) == (2, 1.0, 1.0)


# ── pg-projector 배선 스모크 ───────────────────────────────────────────


class StubStore:
    """apply 성공만 흉내낸다 — 배선 검증에 PG는 필요 없다."""

    async def apply(self, envelope: dict) -> bool:
        return True


class StubMsg:
    def __init__(self, envelope: dict) -> None:
        self.data = json.dumps(envelope).encode()
        self.subject = "lf.test.w_main.actor.memory.consolidated"
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


def projector() -> PgProjector:
    return PgProjector(Config(nats_url="", opensearch_url="", env="test"))


async def test_handle_records_lag_and_emits_log(caplog):
    proj = projector()
    proj._lag = LagAggregator(window=1)  # 매 건 발화 — 스모크에 충분하다
    msg = StubMsg(sample("actor.memory.consolidated"))
    with caplog.at_level(logging.INFO, logger="lf.projector.pg"):
        await proj._handle(msg, StubStore(), js=None)
    assert msg.acked
    [line] = [r.getMessage() for r in caplog.records if "projection_lag_seconds" in r.getMessage()]
    assert "max=" in line and "count=1" in line


async def test_handle_survives_missing_occurred_at(caplog):
    # 계측 실패는 부수 관찰의 실패일 뿐 — ack 경로가 살아야 한다
    envelope = sample("actor.memory.consolidated")
    del envelope["occurred_at"]
    msg = StubMsg(envelope)
    with caplog.at_level(logging.INFO, logger="lf.projector.pg"):
        await projector()._handle(msg, StubStore(), js=None)
    assert msg.acked
    assert not [r for r in caplog.records if "projection_lag_seconds" in r.getMessage()]


# ── redis/kuzu/os 배선 스모크 — 같은 observe가 각자의 logger로 발화한다 ──


def _lag_line(caplog) -> str:
    [line] = [r.getMessage() for r in caplog.records if "projection_lag_seconds" in r.getMessage()]
    return line


async def test_redis_handle_records_lag(caplog):
    proj = RedisProjector(Config(nats_url="", opensearch_url="", env="test"))
    proj._lag = LagAggregator(window=1)
    msg = StubMsg(sample("feed.post.published"))

    async def apply(envelope: dict) -> None:
        pass

    with caplog.at_level(logging.INFO, logger="lf.projector.redis"):
        await proj._handle(msg, apply, js=None)
    assert msg.acked
    assert "count=1" in _lag_line(caplog)


async def test_kuzu_handle_records_lag(tmp_path, caplog):
    proj = KuzuProjector(
        Config(nats_url="", opensearch_url="", env="test", kuzu_dir=str(tmp_path / "kuzu"))
    )
    proj._lag = LagAggregator(window=1)
    msg = StubMsg(sample("relationship.state.changed"))
    try:
        with caplog.at_level(logging.INFO, logger="lf.projector.kuzu"):
            await proj._handle(msg, js=None)
    finally:
        proj.graph.close()
    assert msg.acked
    assert "count=1" in _lag_line(caplog)


class StubIndex:
    async def bulk_upsert(self, docs: list[dict]) -> None:
        pass


async def test_os_batch_records_lag(caplog):
    proj = OsProjector(Config(nats_url="", opensearch_url="", env="test"))
    proj._lag = LagAggregator(window=1)
    msg = StubMsg(sample("feed.post.published"))
    with caplog.at_level(logging.INFO, logger="lf.projector.os"):
        indexed = await proj.project_batch([msg], StubIndex(), js=None)
    assert indexed == 1
    assert msg.acked
    assert "count=1" in _lag_line(caplog)


# ── Prometheus 지표면 — 옵트인 히스토그램/카운터 (ADR-020 §1 후속) ──────

QUIET = logging.getLogger("lf.test.lag")


def kind_metrics(kind: str) -> tuple[CollectorRegistry, KindMetrics]:
    """격리 registry에 실린 kind 손잡이 — 전역 REGISTRY 오염 없이 표본을 센다."""
    registry = CollectorRegistry()
    return registry, LagMetrics(registry=registry).for_kind(kind)


def lag_count(registry: CollectorRegistry, kind: str) -> float | None:
    return registry.get_sample_value("projection_lag_seconds_count", {"kind": kind})


def test_metrics_histogram_and_counter_accumulate():
    registry, metrics = kind_metrics("pg")
    envelope = {"occurred_at": datetime.now(UTC).isoformat()}
    observe(LagAggregator(), envelope, QUIET, metrics=metrics)
    observe(LagAggregator(), envelope, QUIET, metrics=metrics)
    assert lag_count(registry, "pg") == 2.0
    assert registry.get_sample_value("projection_events_total", {"kind": "pg"}) == 2.0


def test_metrics_budget_boundary_bucket():
    # 예산 2s(ADR-020)가 버킷 경계다 — 큰 위반은 2.0 이하 버킷에 잡히지 않아야 한다
    assert 2.0 in LAG_BUCKETS
    registry, metrics = kind_metrics("pg")
    observe(LagAggregator(), {"occurred_at": "2020-01-01T00:00:00Z"}, QUIET, metrics=metrics)
    bucket = {"kind": "pg", "le": "2.0"}
    assert registry.get_sample_value("projection_lag_seconds_bucket", bucket) == 0.0
    bucket["le"] = "+Inf"
    assert registry.get_sample_value("projection_lag_seconds_bucket", bucket) == 1.0


def test_metrics_count_events_even_when_lag_unmeasurable():
    # 처리 건수는 occurred_at 결손과 무관하다 — 히스토그램만 표본을 거른다
    registry, metrics = kind_metrics("pg")
    observe(LagAggregator(), {}, QUIET, metrics=metrics)
    assert registry.get_sample_value("projection_events_total", {"kind": "pg"}) == 1.0
    assert lag_count(registry, "pg") == 0.0


# ── 지표 배선 스모크 — 주입된 손잡이가 각 프로젝터의 성공 경로에서 표본을 받는다 ──


async def test_pg_handle_feeds_histogram():
    registry, metrics = kind_metrics("pg")
    proj = PgProjector(Config(nats_url="", opensearch_url="", env="test"), metrics=metrics)
    await proj._handle(StubMsg(sample("actor.memory.consolidated")), StubStore(), js=None)
    assert lag_count(registry, "pg") == 1.0
    assert registry.get_sample_value("projection_events_total", {"kind": "pg"}) == 1.0


async def test_redis_handle_feeds_histogram():
    registry, metrics = kind_metrics("redis")
    proj = RedisProjector(Config(nats_url="", opensearch_url="", env="test"), metrics=metrics)

    async def apply(envelope: dict) -> None:
        pass

    await proj._handle(StubMsg(sample("feed.post.published")), apply, js=None)
    assert lag_count(registry, "redis") == 1.0


async def test_kuzu_handle_feeds_histogram(tmp_path):
    registry, metrics = kind_metrics("kuzu")
    proj = KuzuProjector(
        Config(nats_url="", opensearch_url="", env="test", kuzu_dir=str(tmp_path / "kuzu")),
        metrics=metrics,
    )
    try:
        await proj._handle(StubMsg(sample("relationship.state.changed")), js=None)
    finally:
        proj.graph.close()
    assert lag_count(registry, "kuzu") == 1.0


async def test_os_batch_feeds_histogram():
    registry, metrics = kind_metrics("os")
    proj = OsProjector(Config(nats_url="", opensearch_url="", env="test"), metrics=metrics)
    indexed = await proj.project_batch(
        [StubMsg(sample("feed.post.published"))], StubIndex(), js=None
    )
    assert indexed == 1
    assert lag_count(registry, "os") == 1.0


# ── 지표 서버 옵트인 — LF_METRICS_PORT 없으면 아무것도 켜지 않는다 ──────


def test_metrics_server_off_without_port(monkeypatch):
    monkeypatch.delenv("LF_METRICS_PORT", raising=False)
    calls: list = []
    monkeypatch.setattr(projector_main, "start_http_server", lambda *a, **k: calls.append(a))
    assert projector_main.start_metrics_server(CollectorRegistry()) is None
    assert calls == []  # dev 기본 — 로그만, HTTP 서버 미기동


def test_metrics_server_starts_when_port_given(monkeypatch):
    monkeypatch.setenv("LF_METRICS_PORT", "9101")
    calls: list = []
    monkeypatch.setattr(
        projector_main, "start_http_server",
        lambda port, registry=None: calls.append((port, registry)),
    )
    registry = CollectorRegistry()
    metrics = projector_main.start_metrics_server(registry)
    assert metrics is not None
    assert calls == [(9101, registry)]
    # 반환된 지표면이 노출 registry에 실려 있다 — 스크레이프가 곧 이 표본을 본다
    metrics.for_kind("os").events.inc()
    assert registry.get_sample_value("projection_events_total", {"kind": "os"}) == 1.0
