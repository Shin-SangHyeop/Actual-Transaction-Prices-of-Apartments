$ErrorActionPreference = "Stop"
$projectPath = $PSScriptRoot
$pythonCommand = Get-Command python -ErrorAction Stop
$pythonPath = $pythonCommand.Source
$scriptPath = Join-Path $projectPath "run.py"
$taskName = "ApartmentTransactionReport"
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('"' + $scriptPath + '"') -WorkingDirectory $projectPath
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description "아파트 실거래 요약 앱 시작" -Force | Out-Null
Write-Host "로그인 시 자동 실행을 등록했습니다: $taskName"
