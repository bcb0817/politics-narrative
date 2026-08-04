param(
    [switch]$Save,
    [switch]$Notify,
    [switch]$ApplyRemediation
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Bot = Join-Path $Root "local_bot.py"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python virtual environment was not found: $Python"
}

Set-Location $Root
$Arguments = @($Bot, "daily-post-goal")
if ($Save) { $Arguments += "--save" }
if ($Notify) { $Arguments += "--notify" }
if ($ApplyRemediation) { $Arguments += "--apply-remediation" }
& $Python @Arguments
exit $LASTEXITCODE
