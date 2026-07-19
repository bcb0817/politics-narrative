$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
Set-Location $Root

Write-Host "=== Botの状態 ===" -ForegroundColor Cyan
if (Test-Path $Python) {
    & $Python local_bot.py status
} else {
    Write-Host ".venvが見つかりません。" -ForegroundColor Red
}

Write-Host "`n=== スケジュールタスク ===" -ForegroundColor Cyan
Get-ScheduledTask -TaskName "PoliticsNarrativeBot" -ErrorAction SilentlyContinue | Format-List TaskName, State
Write-Host "日次レビュー: Bot本体へ統合済み" -ForegroundColor Cyan

Write-Host "`n=== Botの最新ログ ===" -ForegroundColor Cyan
if (Test-Path ".\logs\bot.log") {
    Get-Content ".\logs\bot.log" -Encoding UTF8 -Tail 40
} else {
    Write-Host "bot.logはまだありません。"
}

Write-Host "`n=== 監視処理の最新ログ ===" -ForegroundColor Cyan
if (Test-Path ".\logs\supervisor.log") {
    Get-Content ".\logs\supervisor.log" -Encoding UTF8 -Tail 20
} else {
    Write-Host "supervisor.logはまだありません。"
}


Write-Host "`n=== 日次レビューの最新ログ ===" -ForegroundColor Cyan
if (Test-Path ".\logs\daily_review.log") {
    Get-Content ".\logs\daily_review.log" -Tail 30 -Encoding UTF8
} else {
    Write-Host "daily_review.logはまだありません。"
}
