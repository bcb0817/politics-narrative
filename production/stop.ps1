$BotTask = "PoliticsNarrativeBot"
Stop-ScheduledTask -TaskName $BotTask -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName "PoliticsNarrativeDailyReview" -ErrorAction SilentlyContinue
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (
    Join-Path $PSScriptRoot "short_video_media_stop.ps1")
Write-Host "Botを停止しました（統合された日次レビューも停止）。" -ForegroundColor Yellow
