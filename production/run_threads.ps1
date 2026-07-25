param(
    [ValidateSet("scheduled", "metrics", "token")]
    [string]$Mode = "scheduled"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Bot = Join-Path $Root "local_bot.py"
Set-Location -LiteralPath $Root

$Command = switch ($Mode) {
    "metrics" { "threads-collect-metrics" }
    "token" { "threads-refresh-token" }
    default { "threads-run" }
}

& $Python $Bot $Command
exit $LASTEXITCODE
