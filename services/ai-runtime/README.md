# lf-ai-runtime — AI Runtime (ADR-018)

모든 모델 호출의 단일 통제 지점. 엔진은 NATS request-reply(`lf.<env>.ai.infer`)로만
호출한다 — SDK 직접 사용 금지. 응답은 output_schema로 검증되고, 위반 시 1회 수정
재시도, 재실패는 명시적 오류로 반환된다.

## 프로바이더

키가 설정된 프로바이더는 전부 등록된다. `LF_AI_PROVIDER`는 프리픽스 없는
라우트가 속하는 **기본** 프로바이더를 고른다.

| 프로바이더 | 키 환경변수 | tier 기본 모델 (hot / warm·system) |
|-----------|------------|-----------------------------------|
| `rule` (기본) | 불요 | 결정적 규칙 행동 — dev/CI, LLM 비용·키 없음 |
| `local` | 불요 | qwen2.5:14b (전 티어 단일 — 아래 참고) |
| `anthropic` | `ANTHROPIC_API_KEY` | claude-opus-4-8 / claude-haiku-4-5 (reflect: claude-sonnet-5) |
| `openai` | `OPENAI_API_KEY` | gpt-5 / gpt-5-mini |
| `gemini` | `GEMINI_API_KEY` (또는 `GOOGLE_API_KEY`) | gemini-2.5-pro / gemini-2.5-flash |
| `deepseek` | `DEEPSEEK_API_KEY` | deepseek-chat |
| `glm` | `GLM_API_KEY` (또는 `ZHIPU_API_KEY`) | glm-4.6 / glm-4-flash |

- Gemini/DeepSeek/GLM은 각 사의 OpenAI 호환 엔드포인트를 쓴다.
  base URL 재정의: `LF_GEMINI_BASE_URL`, `LF_DEEPSEEK_BASE_URL`,
  `LF_GLM_BASE_URL` (GLM 해외 리전 등).

### 로컬 모델 (Ollama / LM Studio)

`local` 프로바이더는 키 없이 항상 등록된다. 기본 엔드포인트는 Ollama
(`http://localhost:11434/v1`)이고, LM Studio는 `LF_LOCAL_BASE_URL=http://localhost:1234/v1`.
compose에서는 컨테이너→호스트 접근을 위해 `host.docker.internal`이 기본이다.

```bash
ollama pull qwen2.5:14b            # 기본 모델 (Q4 ≈ 9GB — 12GB VRAM 적합)
LF_AI_PROVIDER=local               # .env — 로컬을 기본으로
LF_LOCAL_MODEL=exaone3.5:7.8b      # (선택) 전 티어 모델 일괄 교체
```

- **전 티어 단일 모델**인 이유: 12GB VRAM에는 모델 1개 상주가 현실적이고,
  티어별로 다른 모델을 쓰면 호출마다 스왑(언로드/로드)이 tick 예산을 잡아먹는다.
- 12GB 대안: `qwen2.5:14b`(기본, 한국어·JSON 우수) · `exaone3.5:7.8b`(한국어 특화,
  더 빠름) · `gemma3:12b` · `qwen3:14b`(thinking 하이브리드 — 지연 증가 주의).
- 로컬 생성은 느릴 수 있다 — actor 대기 예산 `LF_AI_TIMEOUT_S`를 30~45로 상향 권장.
  초과 시 규칙 폴백으로 tick은 계속 흐른다 (`params.fallback: true`).
- gpt-5/o 계열의 reasoning 지연은 `LF_OPENAI_REASONING_EFFORT`(기본 low)로
  통제한다 — decide류는 깊은 추론이 불필요하고, tick 예산 안에 응답해야 한다.
  actor 쪽 대기 예산은 `LF_AI_TIMEOUT_S`(기본 10초).

**주의**: 아리아의 행동 intent에 "(규칙 행동)"이 보이면 그것은 오류가 아니라
rule 프로바이더로 돌고 있거나(키 미설정), LLM 응답이 `LF_AI_TIMEOUT_S`를
초과해 규칙 폴백이 발동한 것이다 (`params.fallback: true`로 구분).

```bash
# compose (infra/compose/.env 에 추가 — .env는 gitignore 대상)
LF_AI_PROVIDER=openai
OPENAI_API_KEY=sk-...

# 호스트 실행
LF_AI_PROVIDER=openai OPENAI_API_KEY=sk-... \
  uv run --package lf-ai-runtime python -m lf_ai_runtime.main
```

## task × tier → 모델 라우팅

기본 라우팅 표는 기본 프로바이더의 tier 모델로 채워진다 (ADR-018 표 준수).
`LF_MODEL_ROUTES`로 라우트별 재정의 — **"프로바이더:모델" 프리픽스로 혼용 가능**:

```bash
# hot은 Claude, warm은 DeepSeek, 나머지는 기본 프로바이더
LF_MODEL_ROUTES='{"decide_action/hot": "anthropic:claude-opus-4-8", "decide_action/warm": "deepseek:deepseek-chat"}'
```

## 구조화 출력 스키마 변환

이벤트 payload JSON Schema를 그대로 쓰되, 구조화 출력 제약에 맞게 전송본을 변환한다
(`providers._sanitize_schema`): 미지원 키워드 제거, 모든 object에
`additionalProperties: false` 강제, `"type": [A, B]` 유니언 → `anyOf`.
**응답 검증은 항상 원본 스키마로** 수행되므로 게이트 강도는 유지된다.

## 남은 정책 (이후 단계)

세계별 토큰 예산 하드 캡, 서킷브레이커/폴백 모델, Langfuse 트레이싱, embed(bge-m3), MCP 도구 루프.
