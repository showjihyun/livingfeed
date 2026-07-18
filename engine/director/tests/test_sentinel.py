"""세계 감시 배치 검증 — 판정 순수 함수·웹훅 페이로드·exit code 규약.

IO 없이 전부 덮는다: evaluate_*는 순수 함수(리포트/지표 본문/시각 → 위반 목록),
'평가 불가'와 '위반'의 구분이 exit code 규약(위반만 1)의 근거이므로 경계값과
소표본 가드를 중심으로 본다.
"""

from datetime import UTC, datetime, timedelta

from lf_director.sentinel import (
    UNAVAILABLE,
    VIOLATION,
    Finding,
    Thresholds,
    build_parser,
    evaluate_heartbeat,
    evaluate_loop_health,
    evaluate_projection_lag,
    exit_code,
    webhook_payload,
)

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
T = Thresholds()


def health_report(
    *, eligible: int, responded: int, rate: float | None, p95: float | None, day: int = 3
) -> dict:
    """loop_health 리포트의 판정 관련 부분 — 실제 fold 산출물과 동형."""
    return {
        "day": day,
        "responses": {
            "eligible_interventions": eligible,
            "responded_interventions": responded,
            "response_rate": rate,
            "latency_seconds": {"p50": 1.0, "p95": p95, "samples": responded},
        },
    }


def prom_text(*kinds: tuple[str, dict[str, float]]) -> str:
    """kind별 누적 버킷 → Prometheus 노출 본문 (prometheus_client 포맷과 동형)."""
    lines = [
        "# HELP projection_lag_seconds 발생→프로젝션 반영 지연 초",
        "# TYPE projection_lag_seconds histogram",
    ]
    for kind, cumulative in kinds:
        for le, cum in cumulative.items():
            lines.append(f'projection_lag_seconds_bucket{{kind="{kind}",le="{le}"}} {cum}')
        lines.append(
            f'projection_lag_seconds_count{{kind="{kind}"}} {cumulative.get("+Inf", 0.0)}'
        )
        lines.append(f'projection_lag_seconds_sum{{kind="{kind}"}} 1.0')
    return "\n".join(lines) + "\n"


# ── 루프 헬스 판정 ─────────────────────────────────────────────────────


def test_loop_health_quiet_day_is_ok():
    """개입 0의 조용한 날은 위반이 아니다 — 분모가 없으면 판정 재료도 없다."""
    assert evaluate_loop_health(
        health_report(eligible=0, responded=0, rate=None, p95=None), T
    ) == []


def test_loop_health_healthy_day_is_ok():
    assert evaluate_loop_health(
        health_report(eligible=4, responded=3, rate=0.75, p95=120.0), T
    ) == []


def test_loop_health_zero_response_fires_regardless_of_sample():
    """응답 0은 소표본 가드와 무관한 구조 신호다 — 단 1건이라도 알린다."""
    findings = evaluate_loop_health(
        health_report(eligible=1, responded=0, rate=0.0, p95=None), T
    )
    assert [f.status for f in findings] == [VIOLATION]
    assert "무응답" in findings[0].message


def test_loop_health_zero_response_does_not_double_fire_with_rate():
    """응답 0이면서 표본이 많아도 경보는 하나다 — 비율 판정은 부분 응답만 본다."""
    findings = evaluate_loop_health(
        health_report(eligible=5, responded=0, rate=0.0, p95=None), T
    )
    assert len(findings) == 1
    assert findings[0].status == VIOLATION


def test_loop_health_low_rate_fires_at_min_sample():
    """비율 위반은 의무 개입이 min_eligible(3)건 이상이고 부분 응답일 때만."""
    findings = evaluate_loop_health(
        health_report(eligible=3, responded=1, rate=0.3333, p95=10.0), T
    )
    assert [f.status for f in findings] == [VIOLATION]
    assert "1건만" in findings[0].message


def test_loop_health_rate_boundary_is_ok():
    """rate == 하한(0.5)은 위반이 아니다 — 판정은 엄격 미만이다."""
    assert evaluate_loop_health(
        health_report(eligible=4, responded=2, rate=0.5, p95=10.0), T
    ) == []


def test_loop_health_latency_p95_boundary_and_violation():
    """p95 == 상한(600s)은 통과, 넘어서면 위반 — 엄격 초과다."""
    assert evaluate_loop_health(
        health_report(eligible=2, responded=2, rate=1.0, p95=600.0), T
    ) == []
    findings = evaluate_loop_health(
        health_report(eligible=2, responded=2, rate=1.0, p95=601.0), T
    )
    assert [f.status for f in findings] == [VIOLATION]
    assert "601초" in findings[0].message


# ── 프로젝션 lag 판정 ──────────────────────────────────────────────────


def test_lag_within_budget_is_ok():
    text = prom_text(("pg", {"0.5": 100.0, "2.0": 100.0, "+Inf": 100.0}))
    assert evaluate_projection_lag(text, T) == []


def test_lag_p95_beyond_budget_fires_with_kind():
    """95% 순위가 예산(2s) 밖 버킷에 처음 닿으면 위반 — kind가 문장에 실린다."""
    text = prom_text(("pg", {"0.5": 90.0, "2.0": 90.0, "5.0": 100.0, "+Inf": 100.0}))
    findings = evaluate_projection_lag(text, T)
    assert [f.status for f in findings] == [VIOLATION]
    assert "pg" in findings[0].message and "5초" in findings[0].message


def test_lag_p95_boundary_bucket_is_ok():
    """누적 95%가 정확히 예산 버킷(le=2.0)에 차면 p95 <= 2s — 위반 아님(보수적 근사)."""
    text = prom_text(("pg", {"2.0": 95.0, "5.0": 100.0, "+Inf": 100.0}))
    assert evaluate_projection_lag(text, T) == []


def test_lag_p95_in_overflow_bucket_fires():
    """+Inf 버킷에서야 95%가 차면 측정 범위를 넘은 위반이다."""
    text = prom_text(("kuzu", {"2.0": 0.0, "30.0": 90.0, "+Inf": 100.0}))
    findings = evaluate_projection_lag(text, T)
    assert [f.status for f in findings] == [VIOLATION]


def test_lag_mixed_kinds_only_bad_one_fires():
    text = prom_text(
        ("pg", {"0.5": 100.0, "2.0": 100.0, "+Inf": 100.0}),
        ("redis", {"2.0": 50.0, "10.0": 100.0, "+Inf": 100.0}),
    )
    findings = evaluate_projection_lag(text, T)
    assert len(findings) == 1
    assert "redis" in findings[0].message


def test_lag_zero_samples_is_unavailable_not_violation():
    """지표면은 떴는데 표본이 없다 — 정직하게 '평가 불가'다."""
    text = prom_text(("pg", {"0.5": 0.0, "2.0": 0.0, "+Inf": 0.0}))
    findings = evaluate_projection_lag(text, T)
    assert [f.status for f in findings] == [UNAVAILABLE]


def test_lag_metric_absent_is_unavailable():
    findings = evaluate_projection_lag("# HELP other_metric x\nother_metric 1.0\n", T)
    assert [f.status for f in findings] == [UNAVAILABLE]


# ── 심장 박동 판정 ─────────────────────────────────────────────────────


def test_heartbeat_no_ticks_is_unavailable():
    """돈 적 없는 세계는 멎었다고 말할 수 없다 — 평가 불가."""
    findings = evaluate_heartbeat(None, NOW, T)
    assert [f.status for f in findings] == [UNAVAILABLE]


def test_heartbeat_recent_tick_is_ok():
    assert evaluate_heartbeat(NOW - timedelta(minutes=4), NOW, T) == []


def test_heartbeat_silence_at_threshold_fires():
    """침묵 '이상(>=)'이 위반이다 — 세계가 멎은 것을 놓치는 쪽이 최악이다."""
    findings = evaluate_heartbeat(NOW - timedelta(minutes=5), NOW, T)
    assert [f.status for f in findings] == [VIOLATION]
    assert "5.0분" in findings[0].message


# ── 웹훅 페이로드 · exit code 규약 ─────────────────────────────────────


def test_webhook_payload_shape():
    findings = [
        Finding("heartbeat", VIOLATION, "세계가 멎었다"),
        Finding("projection_lag", UNAVAILABLE, "지표 미가동"),
    ]
    payload = webhook_payload("w_test", findings, NOW)
    assert payload == {
        "source": "lf-sentinel",
        "world_id": "w_test",
        "checked_at": "2026-07-18T12:00:00Z",
        "ok": False,
        "violations": [{"check": "heartbeat", "message": "세계가 멎었다"}],
        "unavailable": [{"check": "projection_lag", "message": "지표 미가동"}],
    }


def test_webhook_payload_ok_when_only_unavailable():
    payload = webhook_payload(
        "w_test", [Finding("projection_lag", UNAVAILABLE, "지표 미가동")], NOW
    )
    assert payload["ok"] is True
    assert payload["violations"] == []


def test_exit_code_contract():
    """위반만 1이다 — 정상 0, 평가 불가만 있어도 0 (재료 없음은 위반이 아니다)."""
    assert exit_code([]) == 0
    assert exit_code([Finding("projection_lag", UNAVAILABLE, "지표 미가동")]) == 0
    assert exit_code([
        Finding("projection_lag", UNAVAILABLE, "지표 미가동"),
        Finding("heartbeat", VIOLATION, "세계가 멎었다"),
    ]) == 1


def test_threshold_knobs_env_then_cli(monkeypatch):
    """임계는 env(LF_SENTINEL_*)가 기본을 덮고, CLI 인자가 env를 또 덮는다."""
    monkeypatch.setenv("LF_SENTINEL_RESPONSE_RATE_MIN", "0.7")
    monkeypatch.setenv("LF_SENTINEL_HEARTBEAT_MINUTES", "10")
    args = build_parser().parse_args([])
    assert args.response_rate_min == 0.7
    assert args.heartbeat_minutes == 10.0
    assert args.latency_p95_max == 600.0  # env 미설정 — 코드 기본값
    args = build_parser().parse_args(["--response-rate-min", "0.6"])
    assert args.response_rate_min == 0.6  # CLI가 env를 이긴다
