param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$shortRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
Set-Location -LiteralPath $shortRoot
& $Python -X utf8 local_bot.py short run
exit $LASTEXITCODE
