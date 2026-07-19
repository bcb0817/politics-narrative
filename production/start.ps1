$ErrorActionPreference = "Stop"
$BotTask = "PoliticsNarrativeBot"
Start-ScheduledTask -TaskName $BotTask
Write-Host "Botを開始しました。日次レビューはBot本体が自動実行します。" -ForegroundColor Green
