# lf-ai-runtime — AI Runtime (ADR-018)

모든 모델 호출의 단일 통제 지점. 엔진은 NATS request-reply(`lf.<env>.ai.infer`)로만
호출한다 — SDK 직접 사용 금지. 응답은 output_schema로 검증되고, 위반 시 1회 수정
재시도, 재실패는 명시적 오류로 반환된다.

## 프로바이더

| 프로바이더 | 활성화 | 용도 |
|-----------|--------|------|
| `rule` (기본) | 설정 불요 | 결정적 규칙 행동 — dev/CI, LLM 비용·키 없음 |
| `anthropic` | `LF_AI_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` | 실제 LLM (구조화 출력, 정체성 프리픽스 캐싱) |

**주의**: 아리아의 행동 intent에 "(규칙 행동)"이 보이면 그것은 오류가 아니라
rule 프로바이더로 돌고 있다는 뜻이다. LLM을 쓰려면 위 두 환경변수를 설정하라.

```bash
# compose (infra/compose/.env 에 추가)
LF_AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...

# 호스트 실행
LF_AI_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-... \
  uv run --package lf-ai-runtime python -m lf_ai_runtime.main
```

## task × tier → 모델 (기본값, ADR-018 표)

`LF_MODEL_ROUTES='{"decide_action/hot": "claude-...", ...}'` 로 재정의 — 코드 변경 없이 교체.

| task | tier | 모델 |
|------|------|------|
| decide_action, converse | hot | claude-opus-4-8 |
| decide_action, converse | warm | claude-haiku-4-5 |
| narrate, summarize | system | claude-haiku-4-5 |
| reflect | warm | claude-sonnet-5 |
| director_plan | system | claude-opus-4-8 |

## 구조화 출력 스키마 변환

이벤트 payload JSON Schema를 그대로 쓰되, 구조화 출력 제약에 맞게 전송본을 변환한다
(`providers._sanitize_schema`): 미지원 키워드 제거, 모든 object에
`additionalProperties: false` 강제, `"type": [A, B]` 유니언 → `anyOf`.
**응답 검증은 항상 원본 스키마로** 수행되므로 게이트 강도는 유지된다.

## 남은 정책 (이후 단계)

세계별 토큰 예산 하드 캡, 서킷브레이커/폴백 모델, Langfuse 트레이싱, embed(bge-m3), MCP 도구 루프.
