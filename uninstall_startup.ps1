$taskName = "ApartmentTransactionReport"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "자동 실행 등록을 해제했습니다: $taskName"
} else {
    Write-Host "자동 실행 작업이 등록되어 있지 않습니다."
}
