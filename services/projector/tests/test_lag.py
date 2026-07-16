"""프로젝션 lag 계측 검증 (ADR-020 §1) — 순수 로직 경계 + 전 프로젝터 배선 스모크.

경계의 중심: TZ 정규화(Z/오프셋/결손), 빈 집계의 침묵, 발화 주기(건수·시간)의 리셋.
배선 스모크는 프로젝터마다 하나: 성공 경로가 observe를 지나 ack에 닿는가.
"""

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from lf_projector.config import Config
from lf_projector.kuzu_projector import KuzuProjector
from lf_projector.lag import LagAggregator, lag_seconds
from lf_projector.os_projector import OsProjector
from lf_projector.pg_projector import PgProjector
from lf_projector.redis_projector import RedisProjector

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
