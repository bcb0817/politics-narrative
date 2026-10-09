param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$radarRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
Set-Location -LiteralPath $radarRoot
# Optional separate trigger. Does not install/modify the normal posting task.
& $Python -X utf8 local_bot.py radar tick
exit $LASTEXITCODE
