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
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($taskName, "Start scheduled task")) {
            Start-ScheduledTask -TaskName $taskName
        }
    }
}
