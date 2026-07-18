"""세계 감시 배치 — 루프 헬스·프로젝션 lag·심장 박동을 임계와 비교해 경보로 바꾼다.

실행 (한 번 돌고 끝난다 — cron/CI/수동용, 상주 서비스가 아니다):
    uv run --package lf-director python -m lf_director.sentinel
    uv run --package lf-director python -m lf_director.sentinel \
        --metrics-url http://localhost:9103/metrics

지표는 이미 있다 — loop_health(개입→응답 fold, plan/02 §측정),
projection_lag_seconds(옵트인 Prometheus, ADR-020 §1), es의 tick 스트림.
이 배치가 더하는 것은 "사람이 뒤늦게 발견하지 않게" 하는 배관뿐이다.

판정 규약:
- 위반(violation)        → WARNING 로그 + exit 1 (+ LF_ALERT_WEBHOOK 있으면 JSON POST)
- 평가 불가(unavailable) → 정직하게 보고하되 exit 0 — 재료가 없는 것은 위반이 아니다
- 점검 3종은 독립이다: 하나가 죽어도 나머지는 평가된다.
- 웹훅은 부가 채널이다: 전송 실패는 exit 판정에 영향을 주지 않는다.

판정 로직(evaluate_*)은 순수 함수다 — IO 없이 테스트가 덮는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from psycopg import AsyncConnection

from lf_director.config import Config
from lf_director.loop_health import loop_health

logger = logging.getLogger("lf.director.sentinel")

VIOLATION = "violation"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Finding:
    """점검 하나의 결과 한 건 — message는 사람이 읽는 한국어 문장이다."""

    check: str  # loop_health | projection_lag | heartbeat
    status: str  # violation | unavailable
    message: str


@dataclass(frozen=True)
class Thresholds:
    """경보 임계 — 기본값의 근거는 각 필드 주석에 (CLI/env로 재정의 가능)."""

    #: 응답률 하한 — 의무 개입(DM·댓글)의 절반이 무응답이면 루프(개입→응답→재개입)가
    #: 반도 못 돈다 (plan/02 §측정의 루프 정의에서 온 반분선).
    response_rate_min: float = 0.5
    #: 응답률 판정의 소표본 가드 — 2건 이하에서는 무응답 1건이 비율을 절벽으로
    #: 떨어뜨려(최선이 1/2=0.5) 비율이 신호가 못 된다. 3건부터 비율을 본다.
    min_eligible: int = 3
    #: 첫 응답 p95 상한(초) = 10분 — 정상 리듬에서 응답은 몇 tick(분 단위) 안에
    #: 실린다. 10분을 넘기면 플레이어가 화면을 떠난 뒤다 (plan/02 예산의 상한 프록시).
    latency_p95_max_s: float = 600.0
    #: 프로젝션 반영 예산(초) — ADR-020 §1 "projection lag (전 프로젝터) < 2s".
    lag_p95_budget_s: float = 2.0
    #: tick 침묵 허용(분) — 1 tick = 실시간 60초(기본, ADR-011)이므로 5분 침묵은
    #: 심장 박동 다섯 번이 연속으로 사라진 것이다. 세계가 멎었는데 아무도
    #: 모르는 상황이 최악이라 이 판정만은 '이상(>=)'으로 잡는다.
    heartbeat_minutes: float = 5.0


# ── 판정 — 순수 함수 (IO 없음) ─────────────────────────────────────────


def evaluate_loop_health(report: dict[str, Any], thresholds: Thresholds) -> list[Finding]:
    """loop_health 리포트(최신 완결 시즌-일) → 위반 목록. 순수·결정적."""
    day = report.get("day")
    label = f"{day}일차" if day is not None else "지난 하루"
    responses = report["responses"]
    eligible = responses["eligible_interventions"]
    responded = responses["responded_interventions"]
    rate = responses["response_rate"]
    findings: list[Finding] = []
    if eligible >= 1 and responded == 0:
        # 응답 0은 표본 크기와 무관한 구조 신호다 — 하루 종일 의무 개입이 전부
        # 무응답이면 비율 문제가 아니라 응답 파이프라인 정지를 의심해야 한다
        findings.append(Finding("loop_health", VIOLATION, (
            f"{label}: 응답해야 할 개입 {eligible}건이 하루 종일 무응답이었다"
            " — 루프가 끊겼다"
        )))
    elif eligible >= thresholds.min_eligible and rate is not None \
            and rate < thresholds.response_rate_min:
        # 비율 판정은 소표본 가드 뒤에서만 — 응답 0은 위에서 따로 잡았으므로
        # 여기 오는 것은 부분 응답(responded >= 1)뿐이고, 중복 경보가 없다
        findings.append(Finding("loop_health", VIOLATION, (
            f"{label}: 응답해야 할 개입 {eligible}건 중 {responded}건만 응답받았다"
            f" (기준 {thresholds.response_rate_min:.0%}에 못 미친다)"
        )))
    p95 = responses["latency_seconds"]["p95"]
    if p95 is not None and p95 > thresholds.latency_p95_max_s:
        findings.append(Finding("loop_health", VIOLATION, (
            f"{label}: 느린 응답이 {p95:.0f}초까지 늘어졌다"
            f" (허용 {thresholds.latency_p95_max_s:g}초) — 기다리다 떠날 시간이다"
        )))
    return findings


_BUCKET_RE = re.compile(r"^projection_lag_seconds_bucket\{([^}]*)\}\s+([0-9.eE+\-]+)\s*$")
_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def _lag_buckets(metrics_text: str) -> dict[str, dict[float, float]] | None:
    """Prometheus 본문 → kind별 {버킷 상한: 누적 표본 수}. 히스토그램 부재는 None."""
    per_kind: dict[str, dict[float, float]] = {}
    found = False
    for line in metrics_text.splitlines():
        if line.startswith("projection_lag_seconds"):
            found = True  # bucket/count/sum 표본 라인 — 주석(# HELP/TYPE)은 아니다
        matched = _BUCKET_RE.match(line)
        if matched is None:
            continue
        labels = dict(_LABEL_RE.findall(matched.group(1)))
        upper = labels.get("le")
        if upper is None:
            continue
        le = math.inf if upper == "+Inf" else float(upper)
        per_kind.setdefault(labels.get("kind", "?"), {})[le] = float(matched.group(2))
    return per_kind if found else None


def evaluate_projection_lag(
    metrics_text: str, thresholds: Thresholds, *, source: str = ""
) -> list[Finding]:
    """/metrics 본문 → 위반 목록. 순수·결정적.

    p95는 버킷 계수 근사다: 누적 수가 95% 순위(최근접 순위, loop_health와 동일
    규약)에 처음 닿는 버킷의 상한을 취한다. p95 <= 상한이 보장되므로 상한이
    예산을 넘을 때만 위반 — 확실한 위반만 알리는 보수적 판정이다.
    """
    where = f" ({source})" if source else ""
    per_kind = _lag_buckets(metrics_text)
    if per_kind is None:
        return [Finding("projection_lag", UNAVAILABLE,
                        f"프로젝션 지연 히스토그램이 노출되지 않았다{where} — 평가 불가")]
    findings: list[Finding] = []
    sampled = False
    for kind, buckets in sorted(per_kind.items()):
        total = buckets.get(math.inf, max(buckets.values(), default=0.0))
        if total <= 0:
            continue  # 표본 없는 kind는 판정 재료가 없다
        sampled = True
        rank = math.ceil(0.95 * total)
        p95_le = min((le for le, cum in buckets.items() if cum >= rank), default=math.inf)
        if p95_le > thresholds.lag_p95_budget_s:
            reach = "측정 최상 버킷마저 넘어" if math.isinf(p95_le) else f"{p95_le:g}초선까지 밀려"
            findings.append(Finding("projection_lag", VIOLATION, (
                f"프로젝션({kind})의 반영 지연 95%선이 {reach} 있다"
                f" — 예산은 {thresholds.lag_p95_budget_s:g}초다{where}"
            )))
    if not sampled and not findings:
        return [Finding("projection_lag", UNAVAILABLE,
                        f"프로젝션 지연 표본이 아직 없다{where} — 평가 불가")]
    return findings


def evaluate_heartbeat(
    last_completed_at: datetime | None, now: datetime, thresholds: Thresholds
) -> list[Finding]:
    """최신 system.tick.completed 적재 시각 → 침묵 판정. 순수·결정적."""
    if last_completed_at is None:
        return [Finding("heartbeat", UNAVAILABLE,
                        "완료된 tick이 아직 하나도 없다 — 멎음을 판정할 이력이 없다")]
    last = last_completed_at
    if last.tzinfo is None:  # timestamptz라 항상 aware여야 하나, 방어적 보정
        last = last.replace(tzinfo=UTC)
    silence_min = (now - last).total_seconds() / 60.0
    if silence_min >= thresholds.heartbeat_minutes:
        return [Finding("heartbeat", VIOLATION, (
            f"세계의 심장이 {silence_min:.1f}분째 멎어 있다"
            f" (허용 {thresholds.heartbeat_minutes:g}분) — 마지막 tick 완료 이후 침묵이다"
        ))]
    return []


def webhook_payload(
    world_id: str, findings: list[Finding], checked_at: datetime
) -> dict[str, Any]:
    """경보 웹훅에 실을 JSON — 위반/평가 불가를 나눠 싣는다. 순수·결정적."""
    return {
        "source": "lf-sentinel",
        "world_id": world_id,
        "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
        "ok": not any(f.status == VIOLATION for f in findings),
        "violations": [
            {"check": f.check, "message": f.message}
            for f in findings if f.status == VIOLATION
        ],
        "unavailable": [
            {"check": f.check, "message": f.message}
            for f in findings if f.status == UNAVAILABLE
        ],
    }


def exit_code(findings: list[Finding]) -> int:
    """위반이 하나라도 있으면 1 — 평가 불가만 있거나 전부 정상이면 0 (cron/CI 규약)."""
    return 1 if any(f.status == VIOLATION for f in findings) else 0


# ── IO — 재료 수집 (점검별 독립 실패) ──────────────────────────────────

#: 심장 박동과 최신 완결 시즌-일의 재료를 한 번에 — 완료 tick의 최댓값·최신 적재 시각
_LAST_COMPLETED_SQL = (
    "SELECT max(tick), max(occurred_at) FROM es.events "
    "WHERE world_id = %s AND stream = 'system' AND stream_key = 'tick' "
    "AND type = 'system.tick.completed'"
)


async def _loop_health_findings(
    conn: AsyncConnection, world_id: str, last_tick: int | None,
    interval_ticks: int, thresholds: Thresholds,
) -> list[Finding]:
    """최신 '완결' 시즌-일을 접어 판정한다 — 진행 중인 오늘은 반쪽이라 재지 않는다."""
    day = -1 if last_tick is None else last_tick // interval_ticks - 1
    if day < 0:
        return [Finding("loop_health", UNAVAILABLE,
                        "완결된 시즌-일이 아직 없다 — 첫날이 끝나면 평가한다")]
    report = await loop_health(conn, world_id, day, interval_ticks=interval_ticks)
    return evaluate_loop_health(report, thresholds)


async def collect_db_findings(
    dsn: str, world_id: str, interval_ticks: int, thresholds: Thresholds, now: datetime
) -> list[Finding]:
    """es가 재료인 점검 2종(심장 박동·루프 헬스) — 각각 독립으로 실패한다."""
    try:
        connection = await AsyncConnection.connect(dsn, connect_timeout=5, autocommit=True)
    except Exception:
        logger.warning("이벤트 저장소에 닿지 못했다 — es 기반 점검은 평가 불가", exc_info=True)
        return [
            Finding("heartbeat", UNAVAILABLE,
                    "이벤트 저장소에 닿지 못해 심장 박동을 볼 수 없었다"),
            Finding("loop_health", UNAVAILABLE,
                    "이벤트 저장소에 닿지 못해 지난 하루를 접지 못했다"),
        ]
    findings: list[Finding] = []
    async with connection:
        last_tick: int | None = None
        try:
            row = await (await connection.execute(_LAST_COMPLETED_SQL, (world_id,))).fetchone()
            last_tick, last_at = row if row else (None, None)
            findings += evaluate_heartbeat(last_at, now, thresholds)
        except Exception:
            logger.warning("심장 박동 점검 실패 — 나머지 점검은 계속한다", exc_info=True)
            findings.append(Finding("heartbeat", UNAVAILABLE,
                                    "tick 이력 조회가 실패해 심장 박동을 볼 수 없었다"))
        try:
            findings += await _loop_health_findings(
                connection, world_id, last_tick, interval_ticks, thresholds
            )
        except Exception:
            logger.warning("루프 헬스 점검 실패 — 나머지 점검은 계속한다", exc_info=True)
            findings.append(Finding("loop_health", UNAVAILABLE,
                                    "루프 헬스 조회가 실패해 지난 하루를 접지 못했다"))
    return findings


def lag_findings(urls: list[str], thresholds: Thresholds) -> list[Finding]:
    """프로젝터 /metrics를 GET해 판정한다 — 미지정·미가동은 평가 불가(정직)."""
    if not urls:
        return [Finding("projection_lag", UNAVAILABLE, (
            "지표 주소가 없다 (--metrics-url 또는 LF_SENTINEL_METRICS_URLS)"
            " — 프로젝션 지연은 평가하지 않았다"
        ))]
    findings: list[Finding] = []
    for url in urls:
        try:
            response = httpx.get(url, timeout=5.0)
            response.raise_for_status()
        except Exception:
            findings.append(Finding("projection_lag", UNAVAILABLE,
                                    f"{url} 이 응답하지 않는다 — 지표 미가동은 평가 불가다"))
            continue
        findings += evaluate_projection_lag(response.text, thresholds, source=url)
    return findings


def post_alert(url: str, payload: dict[str, Any]) -> bool:
    """경보 웹훅 POST — 부가 채널: 실패해도 exit 판정에 영향을 주지 않는다."""
    try:
        response = httpx.post(url, json=payload, timeout=5.0)
        response.raise_for_status()
        return True
    except Exception:
        logger.warning("경보 웹훅 전송 실패 — 로그와 exit code가 1차 채널이다", exc_info=True)
        return False


# ── CLI ────────────────────────────────────────────────────────────────


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def build_parser() -> argparse.ArgumentParser:
    """임계 노브 — 기본값은 Thresholds, env(LF_SENTINEL_*)가 덮고, CLI가 또 덮는다."""
    base = Thresholds()
    parser = argparse.ArgumentParser(description="Living Feed 세계 감시 배치 (일회 점검)")
    parser.add_argument("--world", default=None, help="점검 대상 세계 (기본: LF_WORLD_ID)")
    parser.add_argument(
        "--interval-ticks", type=int, default=None,
        help="시즌-일 길이 tick (기본: LF_DIRECTOR_SEASON_TICKS 또는 360)",
    )
    parser.add_argument(
        "--response-rate-min", type=float,
        default=_env_float("LF_SENTINEL_RESPONSE_RATE_MIN", base.response_rate_min),
        help=f"응답률 하한 (기본 {base.response_rate_min})",
    )
    parser.add_argument(
        "--min-eligible", type=int,
        default=_env_int("LF_SENTINEL_MIN_ELIGIBLE", base.min_eligible),
        help=f"응답률 판정에 필요한 최소 의무 개입 수 (기본 {base.min_eligible})",
    )
    parser.add_argument(
        "--latency-p95-max", type=float,
        default=_env_float("LF_SENTINEL_LATENCY_P95_MAX_S", base.latency_p95_max_s),
        help=f"첫 응답 p95 상한 초 (기본 {base.latency_p95_max_s:g})",
    )
    parser.add_argument(
        "--lag-p95-max", type=float,
        default=_env_float("LF_SENTINEL_LAG_P95_BUDGET_S", base.lag_p95_budget_s),
        help=f"프로젝션 반영 p95 예산 초 (기본 {base.lag_p95_budget_s:g}, ADR-020)",
    )
    parser.add_argument(
        "--heartbeat-minutes", type=float,
        default=_env_float("LF_SENTINEL_HEARTBEAT_MINUTES", base.heartbeat_minutes),
        help=f"tick 침묵 허용 분 (기본 {base.heartbeat_minutes:g})",
    )
    parser.add_argument(
        "--metrics-url", action="append", default=None,
        help="프로젝터 /metrics 주소 (반복 가능; 기본: LF_SENTINEL_METRICS_URLS 콤마 구분)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if sys.platform == "win32":
        # psycopg async는 ProactorEventLoop 미지원 (로컬 개발 전용 — prod는 Linux)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    cfg = Config.from_env()
    world_id = args.world or cfg.world_id
    interval_ticks = args.interval_ticks or cfg.season_interval_ticks
    thresholds = Thresholds(
        response_rate_min=args.response_rate_min,
        min_eligible=args.min_eligible,
        latency_p95_max_s=args.latency_p95_max,
        lag_p95_budget_s=args.lag_p95_max,
        heartbeat_minutes=args.heartbeat_minutes,
    )
    urls = args.metrics_url or [
        url.strip()
        for url in os.environ.get("LF_SENTINEL_METRICS_URLS", "").split(",")
        if url.strip()
    ]

    now = datetime.now(UTC)
    findings = asyncio.run(
        collect_db_findings(cfg.pg_dsn, world_id, interval_ticks, thresholds, now)
    )
    findings += lag_findings(urls, thresholds)

    for finding in findings:
        if finding.status == VIOLATION:
            logger.warning("세계 점검 위반 [%s] %s", finding.check, finding.message)
        else:
            logger.info("세계 점검 평가 불가 [%s] %s", finding.check, finding.message)
    payload = webhook_payload(world_id, findings, now)
    logger.info("세계 점검 요약 %s", json.dumps(payload, ensure_ascii=False))

    webhook = os.environ.get("LF_ALERT_WEBHOOK")
    if webhook and not payload["ok"]:
        post_alert(webhook, payload)
    return exit_code(findings)


if __name__ == "__main__":
    raise SystemExit(main())
