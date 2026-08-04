[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("sync", "insights", "search", "daily", "weekly", "token")]
    [string]$Mode
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$bot = Join-Path $root "local_bot.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment was not found."
}

Push-Location $root
try {
    switch ($Mode) {
        "sync" {
            & $python $bot threads-full-sync
        }
        "insights" {
            & $python $bot threads-collect-post-insights
            if ((Get-Date).Hour -eq 2) {
                & $python $bot threads-collect-account-insights
            }
        }
        "search" {
            & $python $bot threads-search --query "政治" --search-type RECENT
            & $python $bot threads-trends
        }
        "daily" {
            & $python $bot threads-daily-report
        }
        "weekly" {
            & $python $bot threads-weekly-report
            & $python $bot threads-x-comparison --days 30
        }
        "token" {
            & $python $bot threads-token-status
            & $python $bot threads-refresh-token
        }
    }
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
