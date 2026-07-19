$BotTask = "PoliticsNarrativeBot"
Stop-ScheduledTask -TaskName $BotTask -ErrorAction SilentlyContinue
Write-Host "Botを停止しました（統合された日次レビューも停止）。" -ForegroundColor Yellow
