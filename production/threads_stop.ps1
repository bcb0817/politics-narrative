$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"
$Helpers = Join-Path $PSScriptRoot "threads_env_helpers.ps1"
$Names = @(
    "PoliticsNarrativeThreads",
    "PoliticsNarrativeThreadsMetrics",
    "PoliticsNarrativeThreadsToken"
)
. $Helpers
Set-ThreadsEnvValues -EnvFile $EnvFile -Updates ([ordered]@{
    THREADS_POST_ENABLED = "false"
})
foreach ($Name in $Names) {
    $Task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($Task) {
        Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskName $Name | Out-Null
    }
}
Write-Host "Threads posting disabled; scheduled tasks stopped and disabled."
