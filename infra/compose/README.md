# infra/compose — Docker Compose 개발환경

**로드맵 3단계에서 작성된다** (ADR-019).

계획:
- `--profile core`: 저장소만 (PostgreSQL, Redis, NATS, Qdrant, OpenSearch, MinIO) — 서비스는 호스트에서 직접 실행
- `--profile full`: 전체 (저장소 + 서비스 + 엔진)
