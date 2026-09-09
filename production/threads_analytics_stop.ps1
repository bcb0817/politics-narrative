[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
$taskNames = @(
    "PoliticsNarrativeThreadsSync",
    "PoliticsNarrativeThreadsInsights",
    "PoliticsNarrativeThreadsSearch",
    "PoliticsNarrativeThreadsDailyReport",
    "PoliticsNarrativeThreadsWeeklyReport",
    "PoliticsNarrativeThreadsTokenRefresh"
)

foreach ($taskName in $taskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $task -and $task.State -eq "Running") {
        if ($PSCmdlet.ShouldProcess($taskName, "Stop scheduled task")) {
            Stop-ScheduledTask -TaskName $taskName
        }
    }
}
