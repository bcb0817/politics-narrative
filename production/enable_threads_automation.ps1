$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Bot = Join-Path $Root "local_bot.py"
$Register = Join-Path $PSScriptRoot "register_threads_tasks.ps1"
$Helpers = Join-Path $PSScriptRoot "threads_env_helpers.ps1"
$TaskNames = @(
    "PoliticsNarrativeThreads",
    "PoliticsNarrativeThreadsMetrics",
    "PoliticsNarrativeThreadsToken"
)

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment not found. Run production\install.ps1 first."
}
if (-not (Test-Path -LiteralPath $Bot)) {
    throw "Bot entrypoint not found: $Bot"
}
. $Helpers

$Updates = [ordered]@{
    THREADS_ENABLED = "true"
    THREADS_POST_ENABLED = "true"
    THREADS_INSIGHTS_ENABLED = "true"
    THREADS_DAILY_POST_MIN = "2"
    THREADS_DAILY_POST_MAX = "3"
    THREADS_POST_SCHEDULE = "08:30,13:00,20:30"
    THREADS_MIN_POST_INTERVAL_MINUTES = "180"
    THREADS_TOPIC_COOLDOWN_HOURS = "8"
    THREADS_MIN_DELAY_AFTER_X_MINUTES = "30"
}

try {
    Set-ThreadsEnvValues -EnvFile $EnvFile -Updates $Updates
    Set-Location -LiteralPath $Root

    $TokenStatus = (& $Python $Bot threads-token-status | Out-String) |
        ConvertFrom-Json
    if (
        -not $TokenStatus.configured -or
        -not $TokenStatus.token_present -or
        $TokenStatus.refresh_required
    ) {
        throw "Threads token is missing or requires refresh."
    }

    $ProfileStatus = (& $Python $Bot threads-profile | Out-String) |
        ConvertFrom-Json
    if (-not $ProfileStatus.token_valid) {
        throw "Threads profile API validation failed."
    }

    & powershell.exe -NoProfile -ExecutionPolicy Bypass `
        -File $Register -Force
    if ($LASTEXITCODE -ne 0) {
        throw "Threads scheduled task registration failed."
    }
    foreach ($Name in $TaskNames) {
        Enable-ScheduledTask -TaskName $Name | Out-Null
    }
} catch {
    $Rollback = [ordered]@{
        THREADS_POST_ENABLED = "false"
    }
    Set-ThreadsEnvValues -EnvFile $EnvFile -Updates $Rollback
    throw
}

Write-Host "Threads automation enabled." -ForegroundColor Green
Write-Host "Posting schedule: 08:30, 13:00, 20:30 JST"
Write-Host "Daily target: 2, hard limit: 3"
Write-Host "Automatic replies, follows, likes, and profile changes remain disabled."
