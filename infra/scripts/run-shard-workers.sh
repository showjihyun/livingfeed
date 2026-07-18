#!/usr/bin/env bash
# 샤드 워커 기동 도구 — run-shard-workers.ps1의 동형 (ADR-012 Phase 2).
#
# 1 워커 = 1 샤드 표준 배치. engine/actor(lf_actor.main)를 샤드별 백그라운드
# 프로세스로 띄운다 — 리더 선출(PG advisory lock)·tick 동기화(Redis 배리어)는
# 워커들이 스스로 한다. 로그: logs/shard-workers/<world>-shard<N>.log
#
# env 기본값은 infra/compose 호스트 관례: PG 5433 · Redis 6380 · NATS 4222
# (컨테이너 내부 5432/6379가 아니다 — compose/.env의 호스트 포트 재정의,
# 과거 사고 원인). 이미 설정된 env(LF_PG_DSN 등)가 있으면 그 값이 이긴다.
#
# 운영 절차(샤드 수 변경·페일오버·솔로 복귀)는 infra/compose/README.md
# "샤드 워커 운영" 절이 런북이다.

set -euo pipefail

NUM_SHARDS=2
WORLD="w_main"
SHARDS=""
DRY_RUN=0

usage() {
  cat <<'EOF'
사용법: run-shard-workers.sh [--num-shards N] [--world w_main] [--shards "0,1"] [--dry-run]

  --num-shards N   세계의 샤드 수 (LF_NUM_SHARDS, 기본 2). 고정 상수 — 값 변경은
                   전 액터 재배치이므로 런북의 "샤드 수 변경 절차"를 따른다
  --world ID       대상 세계 (LF_WORLD_ID, 기본 w_main)
  --shards LIST    띄울 샤드 "0,1" — 비우면 전부. 특정 워커만 재기동하는 부분
                   기동(페일오버 운영)용. 이때도 --num-shards는 나머지 워커와 동일해야 한다
  --dry-run        실행 없이 워커별 기동 명령만 출력

env 기본값(재정의는 env로): LF_PG_DSN(PG 5433) · LF_REDIS_URL(6380) · NATS_URL(4222)
                            · LF_QDRANT_URL(6333) · LF_PERSONAS_DIR(agents/personas) · LF_ENV(dev)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --num-shards) NUM_SHARDS="${2:?--num-shards 값이 없다}"; shift 2 ;;
    --world)      WORLD="${2:?--world 값이 없다}"; shift 2 ;;
    --shards)     SHARDS="${2:?--shards 값이 없다}"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "알 수 없는 인자: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$NUM_SHARDS" in
  ''|*[!0-9]*) echo "--num-shards 는 정수여야 한다: '$NUM_SHARDS'" >&2; exit 2 ;;
esac
if [ "$NUM_SHARDS" -lt 1 ]; then
  echo "--num-shards 는 1 이상이어야 한다: $NUM_SHARDS" >&2; exit 2
fi

# ── 샤드 목록 — 비우면 전부, 지정하면 부분 기동 ─────────────────────────
SHARD_LIST=""
if [ -z "$SHARDS" ]; then
  s=0
  while [ "$s" -lt "$NUM_SHARDS" ]; do
    SHARD_LIST="$SHARD_LIST $s"
    s=$((s + 1))
  done
else
  for tok in $(printf '%s' "$SHARDS" | tr ',' ' '); do
    case "$tok" in
      ''|*[!0-9]*) echo "샤드 번호가 아니다: '$tok' (예: --shards \"0,1\")" >&2; exit 2 ;;
    esac
    if [ "$tok" -ge "$NUM_SHARDS" ]; then
      echo "샤드 $tok 은 범위 밖이다 (0..$((NUM_SHARDS - 1)))" >&2; exit 2
    fi
    case " $SHARD_LIST " in
      *" $tok "*) echo "샤드 $tok 이 중복이다" >&2; exit 2 ;;
    esac
    SHARD_LIST="$SHARD_LIST $tok"
  done
  if [ -z "$SHARD_LIST" ]; then echo "--shards 가 비었다" >&2; exit 2; fi
fi

# 스크립트 위치(infra/scripts) 기준 저장소 루트 — LF_PERSONAS_DIR 상대경로의 기준
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# ── env 기본값 — compose 호스트 포트 관례(5433/6380/4222), 전부 env로 재정의 가능 ──
: "${LF_PG_DSN:=postgresql://livingfeed:livingfeed@localhost:5433/livingfeed}"
: "${NATS_URL:=nats://localhost:4222}"
: "${LF_REDIS_URL:=redis://localhost:6380/0}"
: "${LF_QDRANT_URL:=http://localhost:6333}"
: "${LF_PERSONAS_DIR:=agents/personas}"
: "${LF_ENV:=dev}"
# 그 외 LF_* (LF_AI_TIMEOUT_S, LF_GENESIS 등)는 현재 셸 env가 그대로 상속된다.
LOG_DIR="${LF_SHARD_LOG_DIR:-$REPO_ROOT/logs/shard-workers}"

MODE="분산"
if [ "$NUM_SHARDS" -eq 1 ]; then MODE="솔로 (배리어 없음)"; fi
echo "world=$WORLD num_shards=$NUM_SHARDS mode=$MODE shards=[$(echo $SHARD_LIST | tr ' ' ',')] repo=$REPO_ROOT"
echo

LAUNCHED=0
for shard in $SHARD_LIST; do
  CMD="cd '$REPO_ROOT' && LF_WORLD_ID='$WORLD' LF_NUM_SHARDS='$NUM_SHARDS' LF_SHARDS='$shard' LF_PG_DSN='$LF_PG_DSN' NATS_URL='$NATS_URL' LF_REDIS_URL='$LF_REDIS_URL' LF_QDRANT_URL='$LF_QDRANT_URL' LF_PERSONAS_DIR='$LF_PERSONAS_DIR' LF_ENV='$LF_ENV' uv run --package lf-actor python -m lf_actor.main"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "# ── worker shard=$shard (world=$WORLD, num_shards=$NUM_SHARDS) ──"
    echo "$CMD"
    echo
  else
    mkdir -p "$LOG_DIR"
    LOG="$LOG_DIR/$WORLD-shard$shard.log"
    nohup bash -c "$CMD" >>"$LOG" 2>&1 &
    echo "워커 기동: shard $shard/$NUM_SHARDS world=$WORLD PID=$! log=$LOG"
    LAUNCHED=$((LAUNCHED + 1))
  fi
done

if [ "$DRY_RUN" -eq 1 ]; then
  echo "[DryRun] 실행하지 않았다 — 위 명령을 셸마다 하나씩 붙여넣으면 동일하다."
else
  echo
  echo "기동 완료 — 워커 ${LAUNCHED}개. 관찰 포인트(로그, $LOG_DIR):"
  echo '  "tick engine 리더" / "샤드 팔로워"  — 역할 확정 (리더는 정확히 1개여야 한다)'
  echo '  "리더 신호 침묵"                    — 리더 사망 의심, 팔로워가 리더십 재시도'
  echo '  "샤드 결번"                         — 그 tick에 침묵한 샤드 → 해당 워커 부분 재기동'
fi
