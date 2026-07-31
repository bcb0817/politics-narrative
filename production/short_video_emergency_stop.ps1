$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
& ".\.venv\Scripts\python.exe" ".\local_bot.py" "short-video-emergency-stop"
exit $LASTEXITCODE
