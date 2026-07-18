<#
.SYNOPSIS
샤드 워커 기동 도구 — engine/actor(lf_actor.main)를 샤드별 프로세스로 띄운다 (ADR-012 Phase 2).

.DESCRIPTION
1 워커 = 1 샤드 표준 배치. 각 워커는 새 PowerShell 창에서 돌고, 리더 선출(PG
advisory lock)과 tick 동기화(Redis 배리어)는 워커들이 스스로 한다 — 리더는
기동 순서가 아니라 lock이 정한다.

env 기본값은 infra/compose 호스트 관례를 따른다: PG 5433 · Redis 6380 · NATS
4222 (컨테이너 내부 5432/6379가 아니다 — compose/.env의 호스트 포트 재정의,
과거 사고 원인). 이미 설정된 환경변수(LF_PG_DSN 등)가 있으면 그 값이 이긴다.

운영 절차(샤드 수 변경·페일오버·솔로 복귀)는 infra/compose/README.md
"샤드 워커 운영" 절이 런북이다.

.PARAMETER NumShards
세계의 샤드 수 (LF_NUM_SHARDS, 기본 2). 고정 상수다 — 값 변경은 전 액터
재배치이므로 반드시 런북의 "샤드 수 변경 절차"를 따른다 (부분 불일치 금지).

.PARAMETER World
대상 세계 (LF_WORLD_ID, 기본 w_main).

.PARAMETER Shards
띄울 샤드 목록 "0,1". 비우면 0..NumShards-1 전부. 특정 워커만 재기동하는
부분 기동(페일오버 운영)에 쓴다 — 이때도 NumShards는 나머지 워커와 동일해야 한다.

.PARAMETER DryRun
실행하지 않고 워커별 기동 명령만 출력한다 (복사해 손 실행 가능).

.EXAMPLE
.\run-shard-workers.ps1 -NumShards 2
w_main을 2샤드로 전체 기동 (워커 2개, 각각 새 창).

.EXAMPLE
.\run-shard-workers.ps1 -NumShards 2 -Shards "1"
샤드 1 워커만 재기동 (팔로워 다운 후 부분 기동).

.EXAMPLE
.\run-shard-workers.ps1 -NumShards 3 -World w_test -DryRun
실행 없이 3워커 기동 명령 미리보기.
#>
[CmdletBinding()]
param(
    [int]$NumShards = 2,
    [string]$World = 'w_main',
    [string]$Shards = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Get-EnvOrDefault([string]$Name, [string]$Default) {
    $val = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrEmpty($val)) { return $Default }
    return $val
}

function Escape-SingleQuote([string]$s) {
    return $s.Replace("'", "''")
}

if ($NumShards -lt 1) { throw "NumShards는 1 이상이어야 한다: $NumShards" }
if ([string]::IsNullOrWhiteSpace($World)) { throw "World가 비었다" }

# ── 샤드 목록 — 비우면 전부, 지정하면 부분 기동 ─────────────────────────
if ([string]::IsNullOrWhiteSpace($Shards)) {
    $shardList = @(0..($NumShards - 1))
} else {
    $shardList = @()
    foreach ($tok in $Shards.Split(',')) {
        $t = $tok.Trim()
        if ($t -eq '') { continue }
        $n = 0
        if (-not [int]::TryParse($t, [ref]$n)) {
            throw "샤드 번호가 아니다: '$t' (예: -Shards ""0,1"")"
        }
        if ($n -lt 0 -or $n -ge $NumShards) {
            throw "샤드 $n 은 범위 밖이다 (0..$($NumShards - 1))"
        }
        if ($shardList -contains $n) { throw "샤드 $n 이 중복이다" }
        $shardList += $n
    }
    if ($shardList.Count -eq 0) { throw "-Shards 가 비었다" }
}

# 스크립트 위치(infra/scripts) 기준 저장소 루트 — LF_PERSONAS_DIR 상대경로의 기준
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

# ── env 조립 — compose 호스트 포트 관례(5433/6380/4222), 전부 env로 재정의 가능 ──
$envPairs = [ordered]@{
    LF_WORLD_ID     = $World
    LF_NUM_SHARDS   = "$NumShards"
    LF_PG_DSN       = (Get-EnvOrDefault 'LF_PG_DSN' 'postgresql://livingfeed:livingfeed@localhost:5433/livingfeed')
    NATS_URL        = (Get-EnvOrDefault 'NATS_URL' 'nats://localhost:4222')
    LF_REDIS_URL    = (Get-EnvOrDefault 'LF_REDIS_URL' 'redis://localhost:6380/0')
    LF_QDRANT_URL   = (Get-EnvOrDefault 'LF_QDRANT_URL' 'http://localhost:6333')
    LF_PERSONAS_DIR = (Get-EnvOrDefault 'LF_PERSONAS_DIR' 'agents/personas')
    LF_ENV          = (Get-EnvOrDefault 'LF_ENV' 'dev')
}
# 그 외 LF_* (LF_AI_TIMEOUT_S, LF_GENESIS 등)는 현재 셸 env가 그대로 상속된다.

$mode = '분산'
if ($NumShards -eq 1) { $mode = '솔로 (배리어 없음)' }
Write-Host "world=$World num_shards=$NumShards mode=$mode shards=[$($shardList -join ',')] repo=$repoRoot"
Write-Host ''

$launched = @()
foreach ($shard in $shardList) {
    $assignments = @()
    foreach ($k in $envPairs.Keys) {
        $assignments += ("`$env:{0}='{1}'" -f $k, (Escape-SingleQuote $envPairs[$k]))
    }
    $assignments += ("`$env:LF_SHARDS='{0}'" -f $shard)
    $cmd = ($assignments -join '; ') +
        ("; Set-Location '{0}'; " -f (Escape-SingleQuote $repoRoot)) +
        'uv run --package lf-actor python -m lf_actor.main'

    if ($DryRun) {
        Write-Host "# ── worker shard=$shard (world=$World, num_shards=$NumShards) ──"
        Write-Host $cmd
        Write-Host ''
    } else {
        # -NoExit: 워커가 죽어도 창이 남아 마지막 로그를 볼 수 있다 (페일오버 진단)
        $title = "lf-shard $World $shard/$NumShards"
        $launchCmd = ("`$Host.UI.RawUI.WindowTitle='{0}'; " -f (Escape-SingleQuote $title)) + $cmd
        $proc = Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoLogo', '-NoExit', '-Command', $launchCmd) `
            -WorkingDirectory $repoRoot -PassThru
        Write-Host ("워커 기동: shard {0}/{1} world={2} PID={3}" -f $shard, $NumShards, $World, $proc.Id)
        $launched += $proc.Id
    }
}

if ($DryRun) {
    Write-Host "[DryRun] 실행하지 않았다 — 위 명령을 창마다 하나씩 붙여넣으면 동일하다."
} else {
    Write-Host ''
    Write-Host "기동 완료 — 워커 $($launched.Count)개 (PID: $($launched -join ', ')). 관찰 포인트(로그):"
    Write-Host '  "tick engine 리더" / "샤드 팔로워"  — 역할 확정 (리더는 정확히 1개여야 한다)'
    Write-Host '  "리더 신호 침묵"                    — 리더 사망 의심, 팔로워가 리더십 재시도'
    Write-Host '  "샤드 결번"                         — 그 tick에 침묵한 샤드 → 해당 워커 부분 재기동'
}
