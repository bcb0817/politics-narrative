$ErrorActionPreference = "Stop"
$Names = @(
    "PoliticsNarrativeThreads",
    "PoliticsNarrativeThreadsMetrics",
    "PoliticsNarrativeThreadsToken"
)
foreach ($Name in $Names) {
    Enable-ScheduledTask -TaskName $Name | Out-Null
}
Write-Host "Threads scheduled tasks enabled."
