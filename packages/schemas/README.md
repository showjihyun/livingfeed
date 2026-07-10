# @livingfeed/schemas — 스키마 단일 원천

이 패키지는 Living Feed의 **유일한 스키마 원천**이다 (ADR-001 규칙 1).
이벤트 봉투·payload(JSON Schema)와 발행 권한 매트릭스가 여기 살고,
Python/TypeScript 타입은 전부 여기서 **코드젠으로 파생**된다. 수동 타입 작성 금지.

```
packages/schemas/
├── events/                  # JSON Schema (draft 2020-12) — 원천
│   ├── envelope.schema.json             # 공통 봉투 (ADR-002)
│   └── <stream>.<entity>.<verb>.schema.json  # 타입별 payload
├── permissions.yaml         # 발행 권한 매트릭스 (ADR-017 §2)
├── scripts/generate.py      # → Python (pydantic v2)
├── python/                  # uv 멤버 "lf-schemas" — 생성된 모델 포함
└── typescript/              # 생성된 .d.ts + index.ts
```

## 재생성

```bash
# Python (pydantic v2) — 저장소 루트에서
uv run --package lf-schemas python packages/schemas/scripts/generate.py

# TypeScript
pnpm --filter @livingfeed/schemas generate
```

생성 결과물은 **커밋한다**. CI는 재생성 후 `git diff --exit-code`로 드리프트를 차단한다 (ADR-001).

## 스키마 규칙 (ADR-002/017 요약)

- 이벤트 타입 이름: `<stream>.<entity>.<past-tense-verb>` (예: `actor.action.performed`)
- 진화는 additive-only. breaking 변경은 `schema_version` 증가 + 업캐스터 등록 (`upcasters/`)
- 새 이벤트 타입 추가 PR은 스키마 리뷰 필수
