# lf-director — Director AI + 세계 감시(sentinel)

## Director (상주, ADR-013)

서사 관찰 + 제한된 간접 개입. 시즌-일마다 회고(`retrospect`)와 루프 헬스
(`loop_health`, plan/02 §측정)를 구조화 로그로 남긴다.

```powershell
uv run --package lf-director python -m lf_director.main
```

설정: `LF_PG_DSN`, `NATS_URL`, `LF_ENV`, `LF_WORLD_ID` 등 (`config.py` 참고).

## Sentinel (배치) — 루프 헬스·프로젝션 lag·심장 박동 경보

한 번 돌고 끝나는 점검이다 (상주 아님 — cron/CI/수동용). 이미 있는 지표를
임계와 비교해, 사람이 뒤늦게 발견하지 않도록 경보로 바꾼다.

```powershell
uv run --package lf-director python -m lf_director.sentinel
uv run --package lf-director python -m lf_director.sentinel `
    --metrics-url http://localhost:9103/metrics --heartbeat-minutes 10
```

### 점검 3종 (각각 독립 — 하나가 죽어도 나머지는 평가된다)

| 점검 | 위반 기준(기본) | 노브 (CLI / env) |
|------|-----------------|-------------------|
| 루프 헬스 — 최신 **완결** 시즌-일의 `loop_health` | 응답 의무 개입이 있는데 응답 0 · 응답률 < 0.5 (의무 개입 3건 이상일 때만 — 소표본 오탐 방지) · 첫 응답 p95 > 600s | `--response-rate-min` / `LF_SENTINEL_RESPONSE_RATE_MIN`, `--min-eligible` / `LF_SENTINEL_MIN_ELIGIBLE`, `--latency-p95-max` / `LF_SENTINEL_LATENCY_P95_MAX_S` |
| 프로젝션 lag — 프로젝터 `/metrics`(LF_METRICS_PORT 옵트인)의 `projection_lag_seconds` | p95(버킷 계수 근사) > 2s (ADR-020 §1 예산) | `--lag-p95-max` / `LF_SENTINEL_LAG_P95_BUDGET_S`, `--metrics-url`(반복 가능) / `LF_SENTINEL_METRICS_URLS`(콤마 구분) |
| 심장 박동 — `system.tick.completed` 최신 적재 | 벽시계 침묵 ≥ 5분 (1 tick = 실시간 60초 기본 — 박동 다섯 번이 사라진 것) | `--heartbeat-minutes` / `LF_SENTINEL_HEARTBEAT_MINUTES` |

### 판정 규약

- **exit 0**: 전부 정상, 또는 '평가 불가'만 있음 (재료 없음은 위반이 아니다 —
  metrics 미가동·완결된 날 없음·DB 불달은 정직하게 평가 불가로 보고된다).
- **exit 1**: 위반이 하나라도 있음. 위반은 WARNING 로그(한국어 문장)로도 남는다.
- `LF_ALERT_WEBHOOK`이 설정돼 있으면 위반 시 JSON POST
  (`{source, world_id, checked_at, ok, violations[], unavailable[]}`) —
  부가 채널이라 전송 실패는 exit 판정에 영향 없음.

### cron 예시

```cron
*/5 * * * * cd /srv/livingfeed && uv run --package lf-director python -m lf_director.sentinel >> /var/log/lf/sentinel.log 2>&1
0 6 * * *   cd /srv/livingfeed && LF_SENTINEL_METRICS_URLS=http://localhost:9101/metrics,http://localhost:9102/metrics,http://localhost:9103/metrics,http://localhost:9104/metrics uv run --package lf-director python -m lf_director.sentinel
0 7 * * 0   cd /srv/livingfeed && uv run --package lf-projector python -m lf_projector.main --kind pg --verify
```

1행: 5분마다 심장 박동·루프 헬스 상시 감시. 2행: 하루 한 번 프로젝션 lag까지
전 점검. 3행: 주간 프로젝션 무결성 검사(`--verify`는 kuzu/pg/redis 각각 —
sentinel과 별개의 깊은 검사, 어긋나면 `--rebuild` 판단 근거).
