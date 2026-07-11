# infra/ci — CI/CD

워크플로 정의는 GitHub Actions 관례상 [`.github/workflows/`](../../.github/workflows/)에 있다.
이 문서는 그 개요다 (ADR-001 §툴링, ADR-017 §5, ADR-019 §CI/CD).

## ci.yml — PR + main push

| job | 실행 조건 (경로 필터) | 내용 |
|-----|----------------------|------|
| `changes` | 항상 | dorny/paths-filter로 영향 영역 산출. main push는 필터 무시하고 전부 실행 |
| `js` | `apps/`, `packages/`, JS 루트 설정 | pnpm install → lint / typecheck / test / build (Turborepo) |
| `python` | `services/`, `engine/`, `packages/schemas/`, uv 설정 | uv sync → ruff check → pytest |
| `schema-gate` | `packages/schemas/` | ① 코드젠 재생성 후 드리프트 검사 (ADR-001 규칙 1) ② 샘플 이벤트 하위 호환 재검증 + permissions.yaml 검증 (ADR-017 §5, `scripts/check_compat.py`) |
| `docker` | `Dockerfile.python`, Python 소스 | gateway / feed-api 이미지 빌드. main push 시 GHCR 발행 (`sha-<커밋>` + `latest`) |
| `ci-ok` | 항상 | 집계 job — branch protection의 required check는 이것 하나만 걸면 된다 |

로컬에서 동일 검증:

```bash
pnpm install --frozen-lockfile && pnpm lint && pnpm typecheck && pnpm test && pnpm build
uv sync --frozen && uv run ruff check . && uv run pytest
uv run --package lf-schemas python packages/schemas/scripts/generate.py
pnpm --filter @livingfeed/schemas generate          # 이후 git diff가 비어야 한다
uv run --package lf-schemas python packages/schemas/scripts/check_compat.py
```

## 남은 것 (환경이 생기면 추가)

- main → **staging 자동 배포 + smoke** (tick 3회 완주 + 피드 노출 검증) — staging K8s 구축 후 (ADR-019)
- **prod**: 수동 승인 → 순차 롤아웃 → 카나리(신규 세계 1개) → 전체 (ADR-019)
- Turborepo 원격 캐시 (저장소가 커져 CI 시간이 문제될 때, ADR-001)
