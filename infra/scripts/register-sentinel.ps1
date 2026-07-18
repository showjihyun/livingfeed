# LivingFeed 센티널 정기 구동 등록 — Windows 작업 스케줄러에 반복 작업을 건다.
#
# 사용:
#   .\infra\scripts\register-sentinel.ps1                     # 10분 간격 등록
#   .\infra\scripts\register-sentinel.ps1 -IntervalMinutes 5  # 간격 변경 (재등록)
#   .\infra\scripts\register-sentinel.ps1 -DryRun             # 실행 없이 계획만
#   .\infra\scripts\register-sentinel.ps1 -Unregister         # 등록 해제
#
# 러너(run-sentinel.ps1)가 env 기본값·로그 누적을 맡는다. 웹훅(LF_ALERT_WEBHOOK)
# 등 env 재정의는 사용자 환경 변수로 걸어두면 스케줄러 실행에도 전파된다.
[CmdletBinding()]
param(
    [int]$IntervalMinutes = 10,
    [string]$TaskName = 'LivingFeed Sentinel',
    [switch]$Unregister,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'run-sentinel.ps1'

if ($Unregister) {
    if ($DryRun) {
        "계획: Unregister-ScheduledTask -TaskName '$TaskName'"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    "등록 해제 완료: $TaskName"
    exit 0
}

if ($IntervalMinutes -lt 1) { throw "IntervalMinutes는 1 이상이어야 한다: $IntervalMinutes" }
if (-not (Test-Path $runner)) { throw "러너가 없다: $runner" }

if ($DryRun) {
    "계획: '$TaskName' — ${IntervalMinutes}분 간격, powershell -File `"$runner`""
    "로그: %LOCALAPPDATA%\LivingFeed\sentinel.log"
    exit 0
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`""
# -Once + 반복 간격: 로그인 여부와 무관하게 첫 등록 1분 뒤부터 계속 돈다
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
# 점검은 몇 초짜리 배치다 — 겹침은 무시, 5분 넘게 걸리면 강제 종료(행 방지)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
"등록 완료: '$TaskName' — ${IntervalMinutes}분 간격"
"러너: $runner"
"로그: %LOCALAPPDATA%\LivingFeed\sentinel.log (위반 문장·exit 코드 누적)"
