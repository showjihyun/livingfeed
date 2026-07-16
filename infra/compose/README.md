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
  후, 해당 프로젝터만 `--rebuild`로 재구축한다 (각 서비스 정의의 주석 참고).
- os(OpenSearch)는 verify가 없다 — `_id=event_id` upsert 멱등이라 의심되면 바로
  `--kind os --rebuild`.
- 소비 지연은 배치가 아니라 상시 로그로 본다: 각 프로젝터가
  `projection_lag_seconds max=… avg=… count=…`를 주기 발화한다 (ADR-020 §1, 예산 <2s).

## 구성 메모

- **initdb/**: 첫 기동 시 `es`/`read` 스키마 생성. 볼륨이 이미 있으면 실행되지 않는다 (`down -v` 후 재기동).
- **Dockerfile.python**: 전 Python 서비스 공용. 빌드 컨텍스트는 저장소 루트(uv workspace 때문),
  `PACKAGE`/`APP_MODULE` build arg로 서비스를 선택한다.
- **Qdrant 헬스체크 없음**: 이미지에 shell/curl이 없다. 호스트에서 `GET :6333/readyz`로 확인.
- **JetStream 스트림(LF_ACTOR 등) 프로비저닝**은 Core Engine 단계(로드맵 5)에서 dispatcher가 담당한다.
- Kuzu는 여기 없다 — 임베디드라 projector 프로세스 안에 산다 (ADR-006).
