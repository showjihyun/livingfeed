# Living Feed

> AI가 살아가는 세상을 탐험하는 **Interactive Social Drama Platform**

Living Feed는 AI 사회 속에서 사람들과 관계를 맺고, 그들의 인생에 개입하며
세상을 변화시키는 모바일 게임/플랫폼이다.

## Core Fantasy

- Every AI lives.
- Every action leaves a scar.
- Relationships create stories.
- Attention changes reality.
- The world never stops.

## Architecture at a Glance

| 축 | 결정 | ADR |
|----|------|-----|
| 상태 모델 | Event Sourcing — 모든 변화는 불변 이벤트, PostgreSQL이 SoT | [002](docs/adr/ADR-002-event-sourcing.md), [005](docs/adr/ADR-005-postgresql-source-of-truth.md) |
| 읽기 모델 | CQRS + 재구축 가능한 프로젝션 (Kuzu/Qdrant/OpenSearch/Redis) | [003](docs/adr/ADR-003-cqrs-projection.md) |
| 메시징 | NATS JetStream + transactional outbox + Event Dispatcher | [004](docs/adr/ADR-004-nats-jetstream.md), [017](docs/adr/ADR-017-event-dispatcher.md) |
| 기억 | 5계층 Memory Fabric + Context Fabric (토큰 예산 조립) | [008](docs/adr/ADR-008-memory-fabric.md), [009](docs/adr/ADR-009-context-fabric.md) |
| 시뮬레이션 | 이산 tick(60s) + 액터 LOD(Hot/Warm/Cold) + asyncio 액터 런타임 | [011](docs/adr/ADR-011-tick-engine.md), [012](docs/adr/ADR-012-actor-runtime.md) |
| 서사 | Director AI — 제한된 간접 개입만 | [013](docs/adr/ADR-013-director-ai.md) |
| AI | AI Runtime — task×tier 모델 라우팅, 예산 하드 캡 | [018](docs/adr/ADR-018-ai-runtime-layer.md) |
| 전송 | SSE(피드) + WebSocket(상호작용) hybrid | [010](docs/adr/ADR-010-websocket-sse-hybrid.md) |
| 배포 | dev=Compose, prod=K8s+Helm | [019](docs/adr/ADR-019-deployment-architecture.md) |

전체 결정 기록: **[docs/adr/](docs/adr/README.md)** (ADR-001~020)

## Repository Structure (ADR-001)

```
apps/          # Next.js 웹 (Phase 2: React Native)
services/      # FastAPI 서비스 — gateway, feed-api, dispatcher, projector, ai-runtime
engine/        # 시뮬레이션 코어 — tick, actor, director, emotion, relationship, feed
agents/        # AI Actor 정의 — personas, prompts, MCP tools
packages/      # 공유 — schemas(★ 스키마 단일 원천), ui, api-client
infra/         # compose, ci, k8s
docs/          # ADR, 기획(plan)
```

## Getting Started

요구: Node ≥ 22 (pnpm 10), Python 3.12 (uv), Docker

```bash
# JS/TS
pnpm install
pnpm typecheck

# Python
uv sync
uv run pytest

# 스키마 코드젠 (packages/schemas가 단일 원천 — 산출물은 커밋 대상)
uv run --package lf-schemas python packages/schemas/scripts/generate.py
pnpm --filter @livingfeed/schemas generate
```

Docker Compose 개발환경은 로드맵 3단계에서 추가된다 (`infra/compose/`).

## Roadmap

1. ✅ ADR-001~020 작성
2. ✅ Monorepo 스캐폴드
3. ⬜ Docker Compose 개발환경
4. ⬜ CI/CD (GitHub Actions)
5. ⬜ Core Engine (이벤트 스토어 + outbox + dispatcher → tick)
6. ⬜ 첫 번째 AI Actor 실행
7. ⬜ Living Feed MVP

Phase 목표: Phase 1 — 100 액터 / Phase 2 — 1,000 / Phase 3 — 10,000+ ([ADR-020](docs/adr/ADR-020-performance-budget.md))

## License

TBD (오픈소스 공개 시 결정)
