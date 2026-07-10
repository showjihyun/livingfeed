# infra

배포·개발환경 정의 (ADR-019).

```
infra/
├── compose/    # Docker Compose 개발환경 — 로드맵 3단계 (--profile core|full)
├── ci/         # GitHub Actions 워크플로 정의 — 로드맵 4단계
└── k8s/        # Helm 차트 — staging/prod (values만 분기, 매니페스트 분기 금지)
```

원칙 (ADR-019):
- local = prod 동형: clone 후 한 명령으로 전체 세계 기동
- SoT(PostgreSQL)만 관리형 — 나머지 저장소는 프로젝션 소모품이라 클러스터 내 운영
- 배포 중에도 tick은 멈추지 않는다 (tick 경계 leader 인계, 샤드 순차 재배치)
