#!/usr/bin/env bash
# 백엔드 앱 서비스 전체 기동 — run-backend.ps1의 동형 (macOS·Linux용).
#
# 호스트 프로세스로 11개 서비스를 띄운다 (dispatcher/ai-runtime/projector×4/
# feed-composer/director/gateway/feed-api/actor). 저장소(compose core)는 이미
# 떠 있다고 가정한다.
#
# ps1은 서비스마다 새 PowerShell 창을 띄워 로그를 보여주지만, POSIX 셸에는
# 대응물이 없다. 그래서 run-shard-workers.sh와 같은 방식을 쓴다 — nohup 백그라운드
# + 서비스별 로그 파일(logs/backend/<서비스>.log). 로그는 tail로 본다.
#
# env는 infra/compose 호스트 포트 관례를 따른다: PG 5433 · Redis 6380
# (gateway/actor/director=DB0, feed-api/redis-projector=DB1) · NATS 4222 ·
# OpenSearch 9200 · Qdrant 6333. 컨테이너 내부 포트(5432/6379)가 아니다.
#
# 세계 규모·LLM은 옵션으로 조절한다 (LF_MAX_ACTORS·LF_HOT_START_ACTORS).
# 로컬 LLM(local 프로바이더)은 ollama :11434를 기본으로 쓴다 — 서버가 떠 있어야
# 반응이 LLM으로 생성된다(아니면 규칙 폴백). GPU 부담은 --hot-start·--max-actors
# ·--ai-concurrency로 조인다.

set -euo pipefail

MAX_ACTORS=15
HOT_START=6
NUM_SHARDS=1
MODE="idle"
AI_PROVIDER="local"
LOCAL_MODEL="qwen3:8b"
AI_CONCURRENCY=4
DRY_RUN=0

usage() {
  cat <<'EOF'
사용법: run-backend.sh [옵션]

  --max-actors N      세계에 깨울 액터 수 (LF_MAX_ACTORS, 기본 15 — 로컬 LLM 실측 범위)
  --hot-start N       초기 Hot 액터 상한 (LF_HOT_START_ACTORS, 기본 6 — tick당 LLM 상한)
  --num-shards N      액터 샤드 수 (LF_NUM_SHARDS, 기본 1 — 솔로)
  --mode MODE         세계 활기 (LF_WORLD_MODE): idle=유휴 저전력(개입할 때만 LLM,
                      GPU 최소, 기본) / lively=상시 Hot 바닥으로 ambient 활동(GPU↑)
  --ai-provider P     LLM 프로바이더 (rule=GPU 없음 / local=ollama, 기본 local.
                      그 외: anthropic·openai·gemini·deepseek·glm)
  --local-model M     local 프로바이더 모델 (LF_LOCAL_MODEL, 기본 qwen3:8b)
  --ai-concurrency N  동시 LLM 호출 상한 (LF_AI_CONCURRENCY, 기본 4)
  --dry-run           실행하지 않고 서비스별 기동 명령만 출력

예:
  run-backend.sh                            # 15명·Hot6·1샤드·local(qwen3:8b)
  run-backend.sh --ai-provider rule         # GPU 없이 (규칙 반응)
  run-backend.sh --max-actors 10 --hot-start 4 --dry-run
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --max-actors)     MAX_ACTORS="${2:?--max-actors 값이 없다}"; shift 2 ;;
    --hot-start)      HOT_START="${2:?--hot-start 값이 없다}"; shift 2 ;;
    --num-shards)     NUM_SHARDS="${2:?--num-shards 값이 없다}"; shift 2 ;;
    --mode)           MODE="${2:?--mode 값이 없다}"; shift 2 ;;
    --ai-provider)    AI_PROVIDER="${2:?--ai-provider 값이 없다}"; shift 2 ;;
    --local-model)    LOCAL_MODEL="${2:?--local-model 값이 없다}"; shift 2 ;;
    --ai-concurrency) AI_CONCURRENCY="${2:?--ai-concurrency 값이 없다}"; shift 2 ;;
    --dry-run)        DRY_RUN=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *) echo "알 수 없는 인자: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# ── 인자 검증 — ps1의 [int]·[ValidateSet]에 해당한다 ─────────────────────────
for pair in "--max-actors:$MAX_ACTORS" "--hot-start:$HOT_START" \
            "--num-shards:$NUM_SHARDS" "--ai-concurrency:$AI_CONCURRENCY"; do
  flag="${pair%%:*}"; val="${pair#*:}"
  case "$val" in
    ''|*[!0-9]*) echo "$flag 는 정수여야 한다: '$val'" >&2; exit 2 ;;
  esac
  if [ "$val" -lt 1 ]; then echo "$flag 는 1 이상이어야 한다: $val" >&2; exit 2; fi
done

case "$MODE" in
  idle|lively) ;;
  *) echo "--mode 는 idle|lively 중 하나여야 한다: '$MODE'" >&2; exit 2 ;;
esac

case "$AI_PROVIDER" in
  rule|local|anthropic|openai|gemini|deepseek|glm) ;;
  *) echo "--ai-provider 가 목록에 없다: '$AI_PROVIDER'" >&2; exit 2 ;;
esac

if [ "$HOT_START" -gt "$MAX_ACTORS" ]; then
  echo "--hot-start($HOT_START) 는 --max-actors($MAX_ACTORS) 를 넘을 수 없다" >&2; exit 2
fi

# 스크립트 위치(infra/scripts) 기준 저장소 루트 — LF_PERSONAS_DIR 상대경로의 기준
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# ── 호스트 포트 관례 (compose/.env: PG 5433 · Redis 6380 · NATS 4222) ─────────
PG="postgresql://livingfeed:livingfeed@localhost:5433/livingfeed"
NATS="nats://localhost:4222"
REDIS0="redis://localhost:6380/0"   # gateway·actor·director (세션/프레즌스/메일박스)
REDIS1="redis://localhost:6380/1"   # feed-api·redis-projector (피드 타임라인)
OS_URL="http://localhost:9200"
QDRANT="http://localhost:6333"

LOG_DIR="${LF_BACKEND_LOG_DIR:-$REPO_ROOT/logs/backend}"
PID_FILE="$LOG_DIR/pids"
LAUNCHED=0

# launch <이름> <uv 패키지> <python -m 인자> [KEY=VALUE ...]
#   공통 env(LF_ENV·LF_WORLD_ID)를 실어 uv run으로 띄운다. 값은 printf %q로
#   이스케이프해 DSN의 특수문자가 셸에 먹히지 않게 한다.
launch() {
  local name="$1" pkg="$2" module="$3"
  shift 3

  local prefix="" kv
  for kv in "LF_ENV=dev" "LF_WORLD_ID=w_main" "$@"; do
    prefix+="$(printf '%q ' "$kv")"
  done
  local cmd="cd $(printf '%q' "$REPO_ROOT") && env ${prefix}uv run --package $pkg python -m $module"

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "# ── $name ──"
    echo "$cmd"
    echo
    return
  fi

  local log="$LOG_DIR/$name.log"
  nohup bash -c "$cmd" >>"$log" 2>&1 &
  echo "$!" >>"$PID_FILE"
  printf '기동: %-16s PID=%s log=%s\n' "$name" "$!" "$log"
  LAUNCHED=$((LAUNCHED + 1))
  sleep 0.4   # 순차 기동 — 소비자·런타임이 액터보다 먼저 붙게
}

echo "백엔드 기동 — 액터 ${MAX_ACTORS}명 · Hot ${HOT_START} · 샤드 ${NUM_SHARDS} · 모드 ${MODE} · LLM ${AI_PROVIDER}(${LOCAL_MODEL})"
echo "repo=$REPO_ROOT"
echo

if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$LOG_DIR"
  : >"$PID_FILE"   # 이번 기동의 PID만 남긴다
fi

# ── 서비스 정의 (기동 순서: 소비자·런타임 먼저, 액터 마지막) ──────────────────
launch dispatcher      lf-dispatcher "lf_dispatcher.main" \
  "LF_PG_DSN=$PG" "NATS_URL=$NATS"

launch ai-runtime      lf-ai-runtime "lf_ai_runtime.main" \
  "NATS_URL=$NATS" "LF_AI_PROVIDER=$AI_PROVIDER" "LF_LOCAL_MODEL=$LOCAL_MODEL" \
  "LF_AI_CONCURRENCY=$AI_CONCURRENCY"

launch os-projector    lf-projector "lf_projector.main --kind os" \
  "OPENSEARCH_URL=$OS_URL" "LF_DATABASE_URL=$PG" "NATS_URL=$NATS"

launch redis-projector lf-projector "lf_projector.main --kind redis" \
  "REDIS_URL=$REDIS1" "LF_DATABASE_URL=$PG" "NATS_URL=$NATS"

launch pg-projector    lf-projector "lf_projector.main --kind pg" \
  "LF_DATABASE_URL=$PG" "NATS_URL=$NATS"

launch kuzu-projector  lf-projector "lf_projector.main --kind kuzu" \
  "LF_DATABASE_URL=$PG" "NATS_URL=$NATS"

launch feed-composer   lf-feed "lf_feed.main" \
  "LF_PG_DSN=$PG" "NATS_URL=$NATS" "LF_PERSONAS_DIR=agents/personas"

launch director        lf-director "lf_director.main" \
  "LF_PG_DSN=$PG" "NATS_URL=$NATS" "LF_REDIS_URL=$REDIS0"

launch gateway         lf-gateway "lf_gateway.main" \
  "PORT=8000" "LF_PG_DSN=$PG" "NATS_URL=$NATS" "REDIS_URL=$REDIS0" \
  "LF_PERSONAS_DIR=agents/personas"

launch feed-api        lf-feed-api "lf_feed_api.main" \
  "PORT=8001" "OPENSEARCH_URL=$OS_URL" "REDIS_URL=$REDIS1" \
  "LF_DATABASE_URL=$PG" "NATS_URL=$NATS"

launch actor           lf-actor "lf_actor.main" \
  "LF_PG_DSN=$PG" "NATS_URL=$NATS" "LF_REDIS_URL=$REDIS0" "LF_QDRANT_URL=$QDRANT" \
  "LF_PERSONAS_DIR=agents/personas" "LF_MAX_ACTORS=$MAX_ACTORS" \
  "LF_HOT_START_ACTORS=$HOT_START" "LF_NUM_SHARDS=$NUM_SHARDS" "LF_WORLD_MODE=$MODE"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "[DryRun] 실행하지 않았다 — 위 명령을 셸마다 하나씩 붙여넣으면 동일하다."
else
  echo
  echo "기동 완료 — ${LAUNCHED}개 서비스."
  echo "확인: curl http://localhost:8000/healthz  (gateway) / http://localhost:8001/healthz (feed-api)"
  echo "로그: tail -f $LOG_DIR/actor.log   # '초기 Hot ${HOT_START}/${MAX_ACTORS}명' 이 보이면 규모 설정이 걸린 것이다"
  echo "정지: kill \$(cat $PID_FILE)"
fi
