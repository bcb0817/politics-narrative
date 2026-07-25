$ErrorActionPreference = "Stop"
$Names = @(
    "PoliticsNarrativeThreads",
    "PoliticsNarrativeThreadsMetrics",
    "PoliticsNarrativeThreadsToken"
)
foreach ($Name in $Names) {
    $Task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($Task) {
        Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskName $Name | Out-Null
    }
}
Write-Host "Threads scheduled tasks stopped and disabled."
