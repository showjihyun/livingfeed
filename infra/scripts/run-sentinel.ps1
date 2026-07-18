# LivingFeed 센티널 1회 실행 래퍼 — 작업 스케줄러가 부른다 (register-sentinel.ps1).
#
# env가 이미 설정돼 있으면 존중하고, 없으면 dev 호스트 관례를 기본으로 쓴다
# (PG 5433 — 5432는 네이티브 PG라 다른 세계다, infra/compose/README.md 포트 맵).
# 출력은 로그 파일에 누적된다 — 위반(exit 1)의 문장은 사람이 읽는 한국어다.
[CmdletBinding()]
param(
    [string]$World = '',
    [string]$LogPath = "$env:LOCALAPPDATA\LivingFeed\sentinel.log"
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)  # infra/scripts → 리포 루트

if (-not $env:LF_PG_DSN) {
    $env:LF_PG_DSN = 'postgresql://livingfeed:livingfeed@localhost:5433/livingfeed'
}
if ($World) { $env:LF_WORLD_ID = $World }
if (-not $env:LF_WORLD_ID) { $env:LF_WORLD_ID = 'w_main' }

$logDir = Split-Path -Parent $LogPath
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

# 래퍼 줄은 ASCII만 — 파이썬(UTF-8)과 한 파일에 섞여도 어느 뷰어에서든 안 깨진다
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
"[$stamp] sentinel start world=$($env:LF_WORLD_ID)" | Out-File -FilePath $LogPath -Append -Encoding utf8

# cmd 경유 리다이렉트 — PS 5.1의 네이티브 stderr 래핑(NativeCommandError)을 피한다
Set-Location $repo
cmd.exe /c "uv run --package lf-director python -m lf_director.sentinel >> `"$LogPath`" 2>&1"
$code = $LASTEXITCODE

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
"[$stamp] sentinel end exit=$code" | Out-File -FilePath $LogPath -Append -Encoding utf8
exit $code
