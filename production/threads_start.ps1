$ErrorActionPreference = "Stop"
$EnableScript = Join-Path $PSScriptRoot "enable_threads_automation.ps1"

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $EnableScript
exit $LASTEXITCODE
