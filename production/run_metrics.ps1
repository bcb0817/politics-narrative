$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
Set-Location $Root
& $Python (Join-Path $Root "local_bot.py") collect-metrics
exit $LASTEXITCODE
