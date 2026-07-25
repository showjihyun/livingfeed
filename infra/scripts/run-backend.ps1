<#
.SYNOPSIS
백엔드 앱 서비스 전체 기동 — 호스트 프로세스로 (dispatcher/ai-runtime/projector×4/
feed-composer/director/actor/gateway/feed-api). 저장소(compose core)는 이미 떠 있다고 가정.

.DESCRIPTION
각 서비스를 새 PowerShell 창(-NoExit)에서 띄운다 (run-shard-workers.ps1와 같은 패턴 —
창이 남아 로그를 본다). env는 infra/compose 호스트 포트 관례를 따른다:
PG 5433 · Redis 6380(gateway/actor/director=DB0, feed-api/redis-projector=DB1) ·
NATS 4222 · OpenSearch 9200 · Qdrant 6333. 컨테이너 내부 포트(5432/6379)가 아니다.

세계 규모·LLM은 파라미터로 조절한다 (capacity.py / LF_MAX_ACTORS·LF_HOT_START_ACTORS).
로컬 LLM(local 프로바이더)은 ollama :11434를 기본으로 쓴다 — 서버가 떠 있어야 반응이
LLM으로 생성된다(아니면 규칙 폴백). GPU 부담은 HotStart·MaxActors·AiConcurrency로 조인다.

.PARAMETER MaxActors     세계에 깨울 액터 수 (LF_MAX_ACTORS, 기본 15 — 로컬 LLM 실측 범위).
.PARAMETER HotStart      초기 Hot 액터 상한 (LF_HOT_START_ACTORS, 기본 6 — tick당 LLM 상한).
.PARAMETER NumShards     액터 샤드 수 (LF_NUM_SHARDS, 기본 1 — 솔로).
.PARAMETER Mode          세계 활기 (LF_WORLD_MODE): idle=유휴 저전력(개입할 때만 LLM, GPU 최소, 기본) / lively=상시 Hot 바닥으로 ambient 활동(GPU↑).
.PARAMETER AiProvider    LLM 프로바이더 (rule=GPU 없음 / local=ollama qwen 등, 기본 local).
.PARAMETER LocalModel    local 프로바이더 모델 (LF_LOCAL_MODEL, 기본 qwen3:8b).
.PARAMETER AiConcurrency 동시 LLM 호출 상한 (LF_AI_CONCURRENCY, 기본 4).
.PARAMETER DryRun        실행하지 않고 서비스별 기동 명령만 출력.

.EXAMPLE
.\run-backend.ps1                                  # 15명·Hot6·1샤드·local(qwen3:8b)
.EXAMPLE
.\run-backend.ps1 -AiProvider rule                 # GPU 없이 (규칙 반응)
.EXAMPLE
.\run-backend.ps1 -MaxActors 10 -HotStart 4 -DryRun
#>
[CmdletBinding()]
param(
    [int]$MaxActors = 15,
    [int]$HotStart = 6,
    [int]$NumShards = 1,
    [ValidateSet('idle', 'lively')]
    [string]$Mode = 'idle',
    [ValidateSet('rule', 'local', 'anthropic', 'openai', 'gemini', 'deepseek', 'glm')]
    [string]$AiProvider = 'local',
    [string]$LocalModel = 'qwen3:8b',
    [int]$AiConcurrency = 4,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

# ── 호스트 포트 관례 (compose/.env: PG 5433 · Redis 6380 · NATS 4222) ─────────
$PG = 'postgresql://livingfeed:livingfeed@localhost:5433/livingfeed'
$NATS = 'nats://localhost:4222'
$REDIS0 = 'redis://localhost:6380/0'   # gateway·actor·director (세션/프레즌스/메일박스)
$REDIS1 = 'redis://localhost:6380/1'   # feed-api·redis-projector (피드 타임라인)
$OS = 'http://localhost:9200'
$QDRANT = 'http://localhost:6333'

# 공통 env — 모든 서비스에 실린다
$common = [ordered]@{ LF_ENV = 'dev'; LF_WORLD_ID = 'w_main' }

# ── 서비스 정의 (기동 순서: 소비자·런타임 먼저, 액터 마지막) ──────────────────
$services = @(
    @{ n = 'dispatcher';      pkg = 'lf-dispatcher'; args = 'lf_dispatcher.main';
       env = @{ LF_PG_DSN = $PG; NATS_URL = $NATS } }
    @{ n = 'ai-runtime';      pkg = 'lf-ai-runtime'; args = 'lf_ai_runtime.main';
       env = @{ NATS_URL = $NATS; LF_AI_PROVIDER = $AiProvider; LF_LOCAL_MODEL = $LocalModel;
                LF_AI_CONCURRENCY = "$AiConcurrency" } }
    @{ n = 'os-projector';    pkg = 'lf-projector'; args = 'lf_projector.main --kind os';
       env = @{ OPENSEARCH_URL = $OS; LF_DATABASE_URL = $PG; NATS_URL = $NATS } }
    @{ n = 'redis-projector'; pkg = 'lf-projector'; args = 'lf_projector.main --kind redis';
       env = @{ REDIS_URL = $REDIS1; LF_DATABASE_URL = $PG; NATS_URL = $NATS } }
    @{ n = 'pg-projector';    pkg = 'lf-projector'; args = 'lf_projector.main --kind pg';
       env = @{ LF_DATABASE_URL = $PG; NATS_URL = $NATS } }
    @{ n = 'kuzu-projector';  pkg = 'lf-projector'; args = 'lf_projector.main --kind kuzu';
       env = @{ LF_DATABASE_URL = $PG; NATS_URL = $NATS } }
    @{ n = 'feed-composer';   pkg = 'lf-feed'; args = 'lf_feed.main';
       env = @{ LF_PG_DSN = $PG; NATS_URL = $NATS; LF_PERSONAS_DIR = 'agents/personas' } }
    @{ n = 'director';        pkg = 'lf-director'; args = 'lf_director.main';
       env = @{ LF_PG_DSN = $PG; NATS_URL = $NATS; LF_REDIS_URL = $REDIS0 } }
    @{ n = 'gateway';         pkg = 'lf-gateway'; args = 'lf_gateway.main';
       env = @{ PORT = '8000'; LF_PG_DSN = $PG; NATS_URL = $NATS; REDIS_URL = $REDIS0;
                LF_PERSONAS_DIR = 'agents/personas' } }
    @{ n = 'feed-api';        pkg = 'lf-feed-api'; args = 'lf_feed_api.main';
       env = @{ PORT = '8001'; OPENSEARCH_URL = $OS; REDIS_URL = $REDIS1;
                LF_DATABASE_URL = $PG; NATS_URL = $NATS } }
    @{ n = 'actor';           pkg = 'lf-actor'; args = 'lf_actor.main';
       env = @{ LF_PG_DSN = $PG; NATS_URL = $NATS; LF_REDIS_URL = $REDIS0; LF_QDRANT_URL = $QDRANT;
                LF_PERSONAS_DIR = 'agents/personas'; LF_MAX_ACTORS = "$MaxActors";
                LF_HOT_START_ACTORS = "$HotStart"; LF_NUM_SHARDS = "$NumShards";
                LF_WORLD_MODE = $Mode } }
)

function Escape-SingleQuote([string]$s) { return $s.Replace("'", "''") }

Write-Host "백엔드 기동 — 액터 ${MaxActors}명 · Hot $HotStart · 샤드 $NumShards · 모드 $Mode · LLM $AiProvider($LocalModel)"
Write-Host "repo=$repoRoot"
Write-Host ''

$launched = @()
foreach ($svc in $services) {
    $env = [ordered]@{}
    foreach ($k in $common.Keys) { $env[$k] = $common[$k] }
    foreach ($k in $svc.env.Keys) { $env[$k] = $svc.env[$k] }

    $assign = @()
    foreach ($k in $env.Keys) {
        $assign += ("`$env:{0}='{1}'" -f $k, (Escape-SingleQuote ([string]$env[$k])))
    }
    $cmd = ($assign -join '; ') +
        ("; Set-Location '{0}'; " -f (Escape-SingleQuote $repoRoot)) +
        ("uv run --package {0} python -m {1}" -f $svc.pkg, $svc.args)

    if ($DryRun) {
        Write-Host "# ── $($svc.n) ──"
        Write-Host $cmd
        Write-Host ''
        continue
    }
    $title = "lf-$($svc.n)"
    $launchCmd = ("`$Host.UI.RawUI.WindowTitle='{0}'; " -f (Escape-SingleQuote $title)) + $cmd
    $proc = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoLogo', '-NoExit', '-Command', $launchCmd) `
        -WorkingDirectory $repoRoot -PassThru
    Write-Host ("기동: {0,-16} PID={1}" -f $svc.n, $proc.Id)
    $launched += $proc.Id
    Start-Sleep -Milliseconds 400   # 순차 기동 — 소비자·런타임이 액터보다 먼저 붙게
}

if ($DryRun) {
    Write-Host "[DryRun] 실행하지 않았다."
} else {
    Write-Host ''
    Write-Host "기동 완료 — $($launched.Count)개 서비스 (PID: $($launched -join ', '))."
    Write-Host "확인: curl http://localhost:8000/healthz  (gateway) / http://localhost:8001/healthz (feed-api)"
    Write-Host "액터 창 로그에 '초기 Hot $HotStart/${MaxActors}명' 이 보이면 규모 설정이 걸린 것이다."
    Write-Host "정지: 각 창을 닫거나, 이 세션에서 백엔드 정지 요청."
}
