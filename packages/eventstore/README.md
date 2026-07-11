# lf-eventstore — 이벤트 스토어 + Transactional Outbox

세계의 모든 상태 변화가 통과하는 **유일한 쓰기 경로** (ADR-002/005).

```
append(conn, principal, events, expected_head=N)
  ① 검증: 봉투·payload JSON Schema + 발행 권한 매트릭스 (ADR-017 §2 — 실패 시 트랜잭션 거부)
  ② stream_heads CAS  — 낙관적 동시성 (충돌 시 ConcurrencyConflict)
  ③ es.events INSERT  — tick 범위 파티션, global_seq 채번
  ④ es.outbox INSERT  — 같은 트랜잭션 (이중 쓰기 문제 원천 차단)
  ⑤ pg_notify('lf_outbox') — relay 웨이크업
```

- 한 번의 `append`는 **단일 스트림**(world_id, stream, stream_key)이 낙관적 잠금 단위다.
- 전달 보장은 at-least-once — 소비자는 `event_id` 멱등 처리 필수 (ADR-005).
- relay(dispatcher)는 `fetch_unpublished` → JetStream publish → `mark_published` → `purge_published`.
- 연결은 `autocommit=True` 권장 — `append`가 자체 트랜잭션을 연다.

## 마이그레이션

```bash
uv run --package lf-eventstore python -m lf_eventstore.migrate \
    postgresql://livingfeed:livingfeed@localhost:5432/livingfeed
```

`es.schema_migrations`로 적용 이력을 추적한다. compose initdb는 스키마 생성만 하고
테이블 정의는 전부 이 러너가 소유한다 — dev/CI/prod 동일 경로 (ADR-019).

## 테스트

실제 PostgreSQL을 사용한다 (compose `--profile core` 또는 CI 서비스 컨테이너).

```bash
docker compose -f infra/compose/docker-compose.yml up -d postgres
uv run pytest packages/eventstore
# 접속 재정의: LF_TEST_DATABASE_URL=postgresql://...
```
