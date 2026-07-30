param(
    [switch]$Execute
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Bot = Join-Path $Root "local_bot.py"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python virtual environment was not found: $Python"
}

Set-Location $Root
if ($Execute) {
    & $Python $Bot measurement-cycle --execute
} else {
    & $Python $Bot measurement-cycle
}
exit $LASTEXITCODE
