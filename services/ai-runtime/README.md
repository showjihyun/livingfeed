# lf-ai-runtime — AI Runtime (ADR-018)

모든 모델 호출의 단일 통제 지점. 엔진은 NATS request-reply(`lf.<env>.ai.infer`)로만
호출한다 — SDK 직접 사용 금지. 응답은 output_schema로 검증되고, 위반 시 1회 수정
재시도, 재실패는 명시적 오류로 반환된다.

## 동시 처리 (LF_AI_CONCURRENCY)

요청은 요청별 asyncio task로 **유계 동시 처리**된다 — 동시 in-flight 상한은
`LF_AI_CONCURRENCY`(기본 4). 샤드 워커(ADR-012 Phase 2)가 병렬로 쏘는 LLM 호출이
인스턴스당 직렬 처리에 막히지 않게 한다. 순서 보장은 없다(요청별 독립 reply
subject라 불필요). 종료(stop) 시 진행 중 요청은 완주 후 내려간다 — 응답 유실 없음.

- 원격 프로바이더(anthropic/openai/…)에는 즉효다 — SDK의 공유 httpx 클라이언트가
  커넥션 풀로 동시 요청을 받는다.
- 상한을 올릴 때는 벤더 rate limit과 tick 예산(`LF_AI_TIMEOUT_S`)을 함께 보라.
  큐에서 기다린 시간도 호출자 대기 시간에 포함된다.

## 프로바이더

키가 설정된 프로바이더는 전부 등록된다. `LF_AI_PROVIDER`는 프리픽스 없는
라우트가 속하는 **기본** 프로바이더를 고른다.

| 프로바이더 | 키 환경변수 | tier 기본 모델 (hot / warm·system) |
|-----------|------------|-----------------------------------|
| `rule` (기본) | 불요 | 결정적 규칙 행동 — dev/CI, LLM 비용·키 없음 |
| `local` | 불요 | qwen3:8b (전 티어 단일 — 아래 참고) |
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
ollama pull qwen3:8b               # 기본 모델 (Q4 ≈ 5GB — 12GB VRAM 여유)
LF_AI_PROVIDER=local               # .env — 로컬을 기본으로
LF_LOCAL_MODEL=qwen2.5:14b         # (선택) 전 티어 모델 일괄 교체
```

- **전 티어 단일 모델**인 이유: 12GB VRAM에는 모델 1개 상주가 현실적이고,
  티어별로 다른 모델을 쓰면 호출마다 스왑(언로드/로드)이 tick 예산을 잡아먹는다.
- 12GB 대안: `qwen3:8b`(기본, 한국어·JSON 우수, Q4 ≈ 5GB) · `qwen2.5:14b`(더 큼, Q4 ≈ 9GB)
  · `exaone3.5:7.8b`(한국어 특화, 더 빠름) · `gemma3:12b`.
- **Qwen3 thinking 주의**: qwen3 계열은 thinking 하이브리드라 기본적으로 추론 토큰을
  뱉어 지연이 커진다. 로컬 provider는 `/no_think` 소프트 스위치로 이를 끈다(구조화
  출력·tick 예산에 유리). 끄기를 원치 않으면 `LF_LOCAL_THINK=1`.
- **Ollama 병렬화**: ai-runtime이 동시로 보내도 Ollama 서버가
  `OLLAMA_NUM_PARALLEL=1`이면 그쪽 큐에서 직렬화된다. 로컬 실병렬은
  `OLLAMA_NUM_PARALLEL=4`(Ollama 서버 env — `LF_AI_CONCURRENCY`와 맞추면 좋다)를
  함께 설정해야 완성된다. 조건은 VRAM 여유다: 병렬 슬롯만큼 KV 캐시가 늘어나므로
  (예: qwen3:8b Q4 ≈ 5GB + 슬롯당 컨텍스트 캐시), 부족하면 Ollama가 슬롯을 줄이거나
  스왑으로 오히려 느려진다. 12GB VRAM에서 qwen3:8b + 4 슬롯은 무난하다.
- 로컬 생성은 느릴 수 있다 — actor 대기 예산 `LF_AI_TIMEOUT_S`를 30~45로 상향 권장.
  초과 시 규칙 폴백으로 tick은 계속 흐른다 (`params.fallback: true`).
- gpt-5/o 계열의 reasoning 지연은 `LF_OPENAI_REASONING_EFFORT`(기본 low)로
  통제한다 — decide류는 깊은 추론이 불필요하고, tick 예산 안에 응답해야 한다.
  actor 쪽 대기 예산은 `LF_AI_TIMEOUT_S`(기본 10초).

**주의**: 규칙 경로의 intent도 사람 문장이라 겉보기로는 LLM과 구분되지 않는다 —
rule 프로바이더인지(키 미설정), LLM 응답이 `LF_AI_TIMEOUT_S`를 초과해 규칙
폴백이 발동했는지는 `decision_trace.tier: cold_rule`(프로바이더)과
`params.fallback: true`(엔진 폴백)로 구분한다.

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

## 비용·레이트 상한 (LLM API 예산 집행)

모든 모델 호출이 `BudgetGuard`(budget.py)를 지난다 — ADR-018 §3·ADR-020 §2의
정책을 그대로 집행한다:

| 상태 | 동작 |
|------|------|
| 지출 < 상한 × 강등비율 | 통과 |
| 지출 ≥ 상한 × 강등비율 (기본 80%) | **티어 강등** — hot 요청이 warm 모델로 나간다 |
| 지출 ≥ 상한 | **명시적 거절** — 액터는 규칙 행동 폴백 (`params.fallback: true`) |
| 분당 호출 > RPM 상한 | 같은 경로로 거절 (벤더 레이트리밋에 부딪히기 전에) |

- **화면에서 조정한다**: 설정 › LLM API (웹 앱 사이드바 톱니) → gateway
  `PUT /admin/ai-limits` → Redis. 가드가 3초 TTL로 읽으므로 **재시작이 필요 없다**.
- **카운터는 Redis에 산다** (`lf:{env}:ai:*`): 무상태 다중 인스턴스에서 세계 단위
  상한을 집행하려면 카운터가 프로세스 밖에 있어야 한다 (ADR-019). Redis가 없으면
  프로세스 안 카운터로 강등되고 경고를 남긴다 — 그때 상한은 인스턴스별로만 걸린다.
- **예산은 세계 단위다**: 요청 `trace.world_id`가 있으면 그 세계의 버킷, 없으면
  `LF_WORLD_ID`(기본 `w_main`). 일·월 경계는 UTC.
- **저장소 장애 시에는 통과시킨다** — 카운터를 못 읽어 추론을 막으면 Redis 장애가
  곧 세계 정지가 된다. 상한이 반드시 걸려야 하는 배포는 Redis 가용성으로 보장하라.

env 바닥값 (Redis 저장본이 없을 때의 유효 한도). **아무것도 설정하지 않으면 일 상한은
$5**다 — 비용 가드의 미설정 기본값은 싼 쪽으로 실패해야 한다(개인 키로 로컬을 돌리다
루프가 밤새 도는 쪽이 훨씬 흔한 사고다). ADR-020 §2의 Phase 1 예산($50/day/세계)은
배포에서 아래 env로 명시한다:

```bash
LF_AI_LIMITS_ENABLED=1      # 0이면 상한 없음
LF_AI_DAILY_USD=5           # 일 상한 USD (0 = 끔). 운영(Phase 1)은 50 (ADR-020 §2)
LF_AI_MONTHLY_USD=0         # 월 상한 USD (0 = 끔)
LF_AI_RPM=60                # 분당 호출 상한 (0 = 끔)
LF_AI_DEGRADE_RATIO=0.8     # 이 비율에서 hot→warm 강등
LF_AI_MAX_OUTPUT_TOKENS=0   # 응답 토큰 상한 (0 = 프로바이더 기본값)
REDIS_URL=redis://localhost:6379/0
LF_WORLD_ID=w_main
```

### 단가 표 (pricing.py)

비용은 모델 단가로 셈한다 (USD/1M tokens, 캐시 읽기·기록 단가 포함). 표는 씨앗이고
벤더 가격표가 바뀌면 `LF_MODEL_PRICES`로 재정의한다:

```bash
LF_MODEL_PRICES='{"gpt-5": {"input": 1.25, "output": 10.0, "cache_read": 0.125}}'
```

- `rule`·`local` 프로바이더는 **비용 0**이다 (토큰당 청구가 없다).
- **미등재 모델은 최상위 티어 단가로 보수적으로 셈하고** 이름을 남긴다
  (설정 화면에 경고로 올라온다). 모르는 모델을 공짜로 셈하면 상한이 조용히
  무력해지기 때문이다 — 실제 단가를 알면 위 env로 바로잡아라.
- ⚠️ 씨앗 표는 2026-07-27 기준이다. `openai` 기본 라우트의 `gpt-5`/`gpt-5-mini`는
  현행 가격표에 없어(모델 세대가 지났다) 미등재로 잡힌다 — 실제 쓰는 모델을
  `LF_MODEL_ROUTES`로 지정하고 단가를 넣어라. `deepseek`·`glm`도 미등재다.

## 남은 정책 (이후 단계)

서킷브레이커/폴백 모델, Langfuse 트레이싱(호출별 비용 추적), embed(bge-m3), MCP 도구 루프.
