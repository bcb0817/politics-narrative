$ErrorActionPreference = "Stop"
$BotTask = "PoliticsNarrativeBot"
$Task = Get-ScheduledTask -TaskName $BotTask -ErrorAction Stop

if ($Task.State -eq "Running") {
    Write-Host "Botは既に実行中です。二重起動はしません。" -ForegroundColor Yellow
    exit 0
}

Start-ScheduledTask -TaskName $BotTask
Write-Host "Botを開始しました。日次レビューはBot本体が自動実行します。" -ForegroundColor Green
