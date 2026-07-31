$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
& ".\.venv\Scripts\python.exe" ".\local_bot.py" "short-video-status"
$statusCode = $LASTEXITCODE
$tasks = foreach ($taskName in @(
    "PoliticsNarrativeShortVideoFactory",
    "PoliticsNarrativeShortVideoQueue"
)) {
    Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
}
$tasks | Select-Object TaskName,State | Format-Table -AutoSize
exit $statusCode
