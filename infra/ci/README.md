# infra/ci — CI/CD

**로드맵 4단계에서 작성된다** (ADR-019 §CI/CD).

계획 (GitHub Actions, `.github/workflows/`):
- PR: 경로 필터 → affected lint/test/build (Turborepo·uv) + 스키마 재생성 diff 게이트 (ADR-001)
- main: staging 자동 배포 → smoke (tick 3회 완주 + 피드 노출 검증)
- prod: 수동 승인 → 카나리(신규 세계 1개) → 전체
