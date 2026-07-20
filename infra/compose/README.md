# infra/compose — Docker Compose 개발환경

로컬 개발용 전체 스택 (ADR-019). prod와 동형(같은 저장소 6종, 같은 이벤트 백본).

## 사용법

```bash
cd infra/compose

docker compose --profile core up -d          # 저장소만 — 서비스는 호스트에서 실행 (권장)
docker compose --profile full up -d --build  # + gateway, feed-api, dispatcher 컨테이너
docker compose ps                            # 상태/헬스 확인
docker compose down                          # 중지 (데이터 볼륨 유지)
docker compose down -v                       # 데이터까지 완전 삭제
```

자격증명 재정의(선택): `.env.example` → `.env` 복사 후 수정. 기본값은 개발 전용.

## 포트 맵

| 서비스 | 포트 | 용도 |
|--------|------|------|
| PostgreSQL | 5432 | SoT — 이벤트 스토어(`es.*`) + 읽기 프로젝션(`read.*`) (ADR-005) |
| Redis | 6379 | Working memory / hot state / 타임라인 캐시 (ADR-008/014) |
| NATS | 4222, 8222(모니터링) | JetStream 이벤트 백본, 단일 노드 (ADR-004) |
| Qdrant | 6333(HTTP), 6334(gRPC) | Semantic memory (ADR-007) |
| OpenSearch | 9200 | 피드 검색 인덱스, 보안 플러그인 off (ADR-014) |
| MinIO | 9000(S3), 9001(콘솔) | Archive 계층 (ADR-008) |
| gateway | 8000 | `full` 프로파일만 |
| feed-api | 8001 | `full` 프로파일만 |
| dispatcher | (포트 없음) | `full` 프로파일만 — outbox relay 워커 (ADR-017). 시작 시 es 마이그레이션 적용 |

## 세계 규모 (액터 수) — `LF_MAX_ACTORS`

세계에 깨워둘 액터 수를 **10~1000명**에서 조절한다. 페르소나 파일(`agents/personas`,
현재 100명)은 그대로 두고, 파일명 순 앞에서 N명만 실린다. 미설정이면 시드 전원.

```bash
LF_MAX_ACTORS=30            # 액터 30명만 (범위 밖은 10/1000으로 클램프)
LF_MODEL_PARAMS_B=8         # (선택) 내 로컬 모델 크기(B) — 하한 위반을 부팅 로그가 경고
LF_HOT_START_ACTORS=8       # 초기 Hot 상한 (기본 8) — tick당 LLM 폭주 방지, 0=전원 Hot
LF_WORLD_MODE=idle          # idle(기본)=유휴 저전력 / lively=상시 활기 (아래)
```

**활기 ↔ 유휴 저전력 (`LF_WORLD_MODE`)** — 세계가 유휴일 때 GPU를 얼마나 쓸지 정한다:
- `idle` (기본, **권장**): 유휴 액터는 Cold로 강등되어 LLM을 쓰지 않는다. **개입(좋아요·
  댓글·DM)할 때만** 대상 액터가 Hot으로 깨어나 LLM으로 반응한다 → 유휴 GPU 거의 0.
- `lively`: 앞의 N명(기본 `LF_HOT_START_ACTORS` 수)을 **상시 Hot으로 고정**해, 개입이 없어도
  계속 글·상호작용을 만든다 → 첫 화면부터 활발하지만 유휴에도 GPU를 계속 쓴다.
- `LF_HOT_FLOOR=6` 으로 상시 Hot 수를 직접 지정할 수도 있다(모드보다 우선).

로컬 LLM은 `idle` 권장. 데모로 활발한 첫 화면을 원하면 `lively`(+ 넉넉한 vRAM).

**GPU/비용 절감의 핵심 — `LF_HOT_START_ACTORS`**: 전원을 Hot으로 시작하면 매 tick
액터 수만큼 LLM을 호출한다(100명 = 100 호출/tick). 기본값 8은 앞 8명만 Hot으로
시작하고 나머지는 Warm(10 tick 케이던스, 유휴 시 Cold로 강등)이라 tick당 LLM 호출을
크게 줄인다. 플레이어 개입·Director 지목을 받은 액터는 여전히 즉시 Hot으로 승격되어
세계는 살아 있다. 로컬 LLM은 이 값을 낮게(예: 4~8) 두는 것을 권장한다.

**LLM 모델 가이드** (운영 경험 기반):

- 로컬 LLM 실측 한계는 **~20명 미만**이다. 그 이상은 반응 지연·품질이 무너진다.
- **20명 이상이면 40B 이상 모델만 권장한다.** `LF_MODEL_PARAMS_B`를 알려주면 액터
  엔진이 부팅 시 하한 위반을 구체적으로 경고한다.
- 400명 이상은 로컬 vRAM보다 호스티드 API(Claude 등, ADR-018 예산 라우팅)를 권장한다.

설정 전에 규모별 권장 모델·vRAM을 미리 확인:

```bash
uv run python scripts/actor_capacity.py --actors 50 --model-b 8   # 내 모델과 대조
uv run python scripts/actor_capacity.py --table                    # 규모별 참고표
```

시드가 목표보다 적으면 더 생성: `uv run python scripts/seed/generate_personas.py --total 200`.

## 샤드 워커 운영 (ADR-012 Phase 2)

액터 tick 워커(`engine/actor`, `lf_actor.main`)는 `LF_NUM_SHARDS`/`LF_SHARDS`로
분산 모드가 된다: 리더 1명(PG advisory lock)이 tick을 시퀀싱하고, 나머지는
**샤드 팔로워**로 리더의 Redis 신호를 받아 자기 샤드만 실행·ack한다. 기동
명령 조립은 `infra/scripts/run-shard-workers.ps1`(Windows) / `.sh`(Linux)가
한다 — 1 워커 = 1 샤드 표준 배치.

env 기본값은 이 디렉터리의 **호스트 포트 관례**를 따른다: PG **5433** · Redis
**6380** · NATS **4222**. 컨테이너 내부 포트(5432/6379)가 아니다 — `.env`의
호스트 포트 재정의이며, 5432/6379로 겨눴다가 호스트의 다른 스택을 맞춘 것이
과거 사고 원인이다. `LF_PG_DSN`/`LF_REDIS_URL`/`NATS_URL` 등 전부 env로
재정의 가능하고, 이미 설정된 env가 스크립트 기본값보다 우선한다.

### 기동 / 부분 재기동 / 증설

```powershell
# 전체 기동 — w_main을 2샤드로 (워커 2개, 각각 새 창에서 로그가 보인다)
.\infra\scripts\run-shard-workers.ps1 -NumShards 2

# 부분 재기동 — 샤드 1 워커가 죽었을 때 그 워커만 (NumShards는 반드시 기존과 동일)
.\infra\scripts\run-shard-workers.ps1 -NumShards 2 -Shards "1"

# 미리보기 — 실행 없이 워커별 명령만 출력 (검증·복사용)
.\infra\scripts\run-shard-workers.ps1 -NumShards 3 -World w_test -DryRun
```

bash 동형: `infra/scripts/run-shard-workers.sh --num-shards 2 [--shards "1"]
[--dry-run]` — 백그라운드 기동, 로그는 `logs/shard-workers/<world>-shard<N>.log`.

- 워커는 전부 동형이다 — 리더는 기동 순서가 아니라 advisory lock이 정한다.
  누가 리더인지는 로그로 확인: `tick engine 리더` vs `샤드 팔로워`.
- **증설/축소**는 두 가지를 구분한다. 같은 `LF_NUM_SHARDS` 안에서 샤드→워커
  배정을 바꾸는 것(워커 증감)은 자유다 — 해당 워커만 정지·부분 기동하면 되고,
  이동 비용은 캐시 웜업뿐이다 (ADR-012 규칙 4). 한 워커가 샤드 여럿을 갖는
  배치(`LF_SHARDS="0,2"`)는 스크립트 범위 밖 — env를 손 조립한다. 반면
  **샤드 수 자체의 변경은 아래 절차의 재배치다.**
- 정지는 워커 프로세스에 Ctrl+C(SIGINT/SIGTERM) — 정상 종료 경로가 있어
  진행 중 tick을 완주하고 내려간다.

### 샤드 수 변경 절차 (LF_NUM_SHARDS — 재배치)

`shard_of = crc32 % num_shards`는 고정 상수다 (ADR-012 규칙 4): num_shards가
바뀌면 **전 액터의 샤드 소속이 바뀐다.** 그래서 이 변경은 설정 수정이 아니라
재배치 작업이고, **부분 불일치 금지**가 철칙이다 — 워커 간 num_shards가
다르면 같은 액터를 두 워커가 소유(이중 실행)하거나 아무도 소유하지 않는
액터(결번)가 생긴다. 절차는 전량 교체다:

1. **전 워커 정지** (Ctrl+C — 진행 중 tick은 완주된다).
2. `LF_NUM_SHARDS` **일괄 변경** — 스크립트 사용 시 `-NumShards` 값 하나다.
3. **전 워커 재기동** — 새 값으로 전 샤드를 한 번에 (`run-shard-workers.ps1 -NumShards <새 값>`).

정지 구간과 실수의 안전망은 시스템이 갖고 있다:

- **tick 결번 없음은 es 복원이 보장한다** — tick 번호의 원천은 이벤트
  스토어(`system/tick` 스트림)다. 재기동한 리더는 마지막 tick부터 이어간다
  (catch-up, ADR-011): 정지 시간 동안 세계시간이 잠시 멈출 뿐, tick 역사에
  공백은 생기지 않는다.
- **이중 실행은 액터 스트림 CAS가 시끄럽게 잡는다** — 만에 하나 num_shards
  불일치로 두 워커가 같은 액터를 실행하면, 액터 스트림 append의
  CAS(expected_head) 충돌로 에러가 난다. 조용한 상태 오염이 아니라 로그에
  보이는 실패이므로, 불일치를 발견하면 전 워커 정지 후 절차를 다시 밟는다.

### 페일오버 동작과 관찰 포인트

| 상황 | 동작 | 로그 문구 |
|------|------|-----------|
| 팔로워 다운 | 리더는 ack 시한(기본 600s)까지 기다린 뒤 그 샤드를 **그 tick의 침묵**으로 두고 진행 — 그동안 tick이 느려진다(미루기 정책) | `샤드 결번` (경고) |
| 팔로워 복귀 | 다운 중 밀린 신호를 버리고 최신 tick부터 따른다 — 스크립트 부분 기동(`-Shards "N"`)으로 재기동 | `밀린 tick 신호 N건 건너뜀` |
| 리더 다운 | 팔로워가 **신호 침묵 4 tick(기본 240s)** 을 감지하면 리더십을 재시도 — 승계한 워커가 es의 마지막 tick부터 재개 | `리더 신호 침묵` → `tick engine 리더` |

관찰 포인트: 정상 분산 기동이면 `tick engine 리더 … [분산: 타 샤드 …]`가
정확히 1개, 나머지는 전부 `샤드 팔로워`다. `샤드 결번` 경고가 매 tick
반복되면 해당 샤드 워커가 죽어 있는 것 — 부분 재기동한다. 배리어 키
(`lf:tickgo:*`/`lf:tickack:*`)는 TTL 24h 소모품이라 청소할 필요가 없다.

### 솔로 복귀 (무위험)

전 워커 정지 후 `LF_NUM_SHARDS` 없이(또는 `1`로) 워커 1개를 띄우면 기존 솔로
모드 그대로다 — 배리어 없이 리더 혼자 전 액터를 돈다. 샤드 배정은 실행
집합의 분할일 뿐 저장된 상태가 아니고(진실은 es), 배리어 키는 TTL 소모품이라
마이그레이션도 청소도 없이 언제든 되돌릴 수 있다. 분산 운영이 의심스러우면
솔로 복귀가 안전한 기본값이다.

## 테스트 인프라 (파괴적 픽스처 격리)

통합 테스트의 픽스처는 **파괴적**이다(es/read 스키마 드롭, LF_* 스트림 삭제,
flushdb). 상주 세계를 지키기 위해 두 겹의 가드가 있다 (`lf_eventstore.testing`,
2026-07-17 사고 2건의 교훈):

1. LF_TEST_* env가 없으면 해당 픽스처는 **skip**이다 — 기본값으로 로컬 인프라를
   겨누지 않는다.
2. env가 있어도 표적을 검증한다: PG는 DB 이름 `_test` 접미 강제, NATS는 마커
   스트림(LF_* 스트림이 전무한 서버에만 자동 생성)이 있는 서버만 허용.
   위반은 skip이 아니라 실패다.

```powershell
# 전용 테스트 표적 — 상주 세계(4222·livingfeed DB)와 완전 분리
$env:LF_TEST_DATABASE_URL='postgresql://livingfeed:livingfeed@localhost:5433/livingfeed_test'
$env:LF_TEST_REDIS_URL='redis://localhost:6380/15'
$env:LF_TEST_NATS_URL='nats://localhost:4223'   # compose nats-test
$env:LF_TEST_QDRANT_URL='http://localhost:6333'
uv run pytest   # 세계가 돌고 있어도 전체 스위트가 안전하다
```

- `livingfeed_test` DB가 없으면: `docker exec livingfeed-postgres-1 psql -U livingfeed -d postgres -c "CREATE DATABASE livingfeed_test;"`
- `nats-test`(4223)는 core 프로파일에 포함 — 볼륨 없는 소모품이라 재기동이 곧 초기화다.

## 주간 프로젝션 무결성 배치 (verify 3종)

프로젝션은 소모품이다 (ADR-003 계약 3): 원천(`es.events`) 대비 어긋남은
**검사(--verify)로 발견하고 재구축(--rebuild)으로 고친다**. 검사는 읽기만
하고 아무것도 고치지 않는다 — 종료 코드 0(무결)/1(어긋남)이 배치의 신호다.

```bash
# full 프로파일 (컨테이너) — 주 1회 cron/Task Scheduler 권장
docker compose run --rm kuzu-projector  python -m lf_projector.main --kind kuzu  --verify
docker compose run --rm pg-projector    python -m lf_projector.main --kind pg    --verify
docker compose run --rm redis-projector python -m lf_projector.main --kind redis --verify

# core 프로파일 (호스트 실행) — LF_* 환경변수로 대상 지정 (config.py 참고)
uv run --package lf-projector python -m lf_projector.main --kind pg --verify [--world w_main]
```

| kind | 비교 대상 | 병행 실행 |
|------|-----------|-----------|
| kuzu | 그래프 엣지 집합 vs es relationship 스트림 키 | **kuzu-projector를 먼저 stop** — Kuzu는 임베디드라 동시 접근이 잠금에 막힌다 |
| pg | read 테이블 키/개수 vs es 이벤트 | 서비스와 병행 안전 (읽기 전용) |
| redis | 팔로워 인덱스·타임라인 vs es 선언 fold | 서비스와 병행 안전 (읽기 전용) |

- 종료 코드 1 → stdout의 JSON 리포트(`mismatched`/`missing`/`extra`)로 원인 확인
  후, 해당 프로젝터만 `--rebuild --once`로 재구축한다: durable과 프로젝션을
  파괴하고 스트림이 유휴해질 때까지 재소비한 뒤 **스스로 종료**하는 일회성
  배치다 (--once 없이는 상시 서비스 루프로 계속 돈다). 재구축 후 같은 verify로
  exit 0을 확인하면 사이클이 닫힌다.
- os(OpenSearch)는 verify가 없다 — `_id=event_id` upsert 멱등이라 의심되면 바로
  `--kind os --rebuild --once`.
- 재구축은 두 원천이 있다: `--rebuild --once`는 **JetStream 스트림**을 재소비
  (보존 한도 내 — 빠른 따라잡기), `--rebuild --from-es`는 **es(SoT)를 직접
  리플레이**한다 — NATS 불요·보존 한도 무관, 스트림이 유실됐어도 전 역사를
  되세운다. from-es는 해당 프로젝터 서비스를 멈추고 돌린다 (재기동 시 durable
  체크포인트부터 이어가고, 겹침은 프로젝션 멱등이 흡수한다).
- 소비 지연은 배치가 아니라 상시 로그로 본다: 각 프로젝터가
  `projection_lag_seconds max=… avg=… count=…`를 주기 발화한다 (ADR-020 §1, 예산 <2s).

### Prometheus 지표 (옵트인, ADR-020 §1 후속)

`LF_METRICS_PORT`를 주면 프로젝터가 `/metrics`를 함께 노출한다 — 미설정이
기본(로그만). 지표는 `projection_lag_seconds{kind}` 히스토그램(버킷
0.05/0.1/0.25/0.5/1/2/5/10/30 — 예산 2s가 경계 버킷)과
`projection_events_total{kind}` 카운터. 한 호스트에 프로젝터 넷이 뜨므로
kind별 포트를 나눠 준다:

| kind | 포트(예) | 기동 |
|------|----------|------|
| os | 9101 | `$env:LF_METRICS_PORT='9101'; uv run --package lf-projector python -m lf_projector.main --kind os` |
| kuzu | 9102 | `--kind kuzu` (LF_METRICS_PORT=9102) |
| pg | 9103 | `--kind pg` (LF_METRICS_PORT=9103) |
| redis | 9104 | `--kind redis` (LF_METRICS_PORT=9104) |

수집기(Prometheus 서버)는 compose에 없다 — 운영 환경의 몫. 스크레이프 예시:

```yaml
scrape_configs:
  - job_name: lf-projector
    static_configs:
      - targets: ["host:9101", "host:9102", "host:9103", "host:9104"]
```

## 구성 메모

- **initdb/**: 첫 기동 시 `es`/`read` 스키마 생성. 볼륨이 이미 있으면 실행되지 않는다 (`down -v` 후 재기동).
- **Dockerfile.python**: 전 Python 서비스 공용. 빌드 컨텍스트는 저장소 루트(uv workspace 때문),
  `PACKAGE`/`APP_MODULE` build arg로 서비스를 선택한다.
- **Qdrant 헬스체크 없음**: 이미지에 shell/curl이 없다. 호스트에서 `GET :6333/readyz`로 확인.
- **JetStream 스트림(LF_ACTOR 등) 프로비저닝**은 Core Engine 단계(로드맵 5)에서 dispatcher가 담당한다.
- Kuzu는 여기 없다 — 임베디드라 projector 프로세스 안에 산다 (ADR-006).
