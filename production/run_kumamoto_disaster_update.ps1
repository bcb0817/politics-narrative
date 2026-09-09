[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("morning", "evening")]
    [string]$SnapshotType
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Bot = Join-Path $Root "local_bot.py"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python virtual environment was not found: $Python"
}

& $Python $Bot disaster-update-full-cycle `
    --incident-id "kumamoto-earthquake-20260728" `
    --snapshot-type $SnapshotType
exit $LASTEXITCODE
