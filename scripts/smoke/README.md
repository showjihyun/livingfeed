# 라이브 스모크 — 실 인프라·실 모델로 사슬 확인

CI가 아니라 **손으로 돌리는 검증**이다: 결정적 테스트(규칙 폴백)가 보증하는
사이클을, 실제 LLM과 실제 메시징 위에서 눈으로 확인한다. 각 스크립트는
매 실행 고유한 `env`/`world_id`를 만들어 기존 데이터를 오염시키지 않는다.

## 준비물

- `infra/compose`의 postgres(5433)·redis(6380)·nats(4222) (+ reflect는 Ollama)
- LLM 스모크(reflect/narrate/sns_lifecycle)는 **Ollama + qwen3:8b** (localhost:11434)
- 재정의: `LF_SMOKE_PG_DSN`, `NATS_URL`, `LF_SMOKE_REDIS_URL`

## 실행 (저장소 루트에서)

| 스크립트 | 사슬 | LLM |
|---|---|---|
| `uv run --package lf-feed python scripts/smoke/arc_chain.py` | 아크 계획 → relay → 전이 피드 → read.actor_arcs → 프로필 → 시즌 회고 | 불필요 |
| `uv run --package lf-feed python scripts/smoke/narrate_boost.py` | 편집 조명(boost_feed) → 경계 행동 승격 + 고드라마 본문 서사화 | qwen3 |
| `uv run --package lf-actor python scripts/smoke/reflect_insight.py` | 작업 기억 → LLM reflection 통찰 (스키마·하드룰 통과) | qwen3 |
| `uv run --package lf-actor python scripts/smoke/sns_lifecycle_llm.py` | 신규 캐릭터 생성 → 정체성 → LLM 행동/답장 — SNS 생활 정성 평가 | qwen3 |

성공 시 `SMOKE: OK` 를 출력하고 0으로 종료한다. LLM 표현 품질은 출력을
직접 읽고 판단한다 — 그게 이 스크립트들이 CI가 아닌 이유다.

결정적 사이클 보증은 `engine/actor/tests/test_persona_sns_lifecycle.py`
(규칙 폴백만으로 9단계 SNS 생활 검증)가 담당한다.
