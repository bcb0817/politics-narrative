$Tasks = @("PoliticsNarrativeBot", "PoliticsNarrativeDailyReview")
foreach ($TaskName in $Tasks) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}
Write-Host "スケジュールタスクを削除しました。コードとデータは保持されています。" -ForegroundColor Yellow
