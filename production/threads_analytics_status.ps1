[CmdletBinding()]
param()

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
    if ($null -eq $task) {
        [pscustomobject]@{
            TaskName = $taskName
            State = "NotRegistered"
            LastRunTime = $null
            NextRunTime = $null
            LastTaskResult = $null
        }
        continue
    }
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    [pscustomobject]@{
        TaskName = $taskName
        State = $task.State
        LastRunTime = $info.LastRunTime
        NextRunTime = $info.NextRunTime
        LastTaskResult = $info.LastTaskResult
    }
}
