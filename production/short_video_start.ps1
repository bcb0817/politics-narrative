$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
Write-Host "Running one scheduled Short Video Factory cycle."
& ".\.venv\Scripts\python.exe" ".\local_bot.py" "short-video-scheduled-run"
exit $LASTEXITCODE
