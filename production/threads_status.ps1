$Names = @(
    "PoliticsNarrativeThreads",
    "PoliticsNarrativeThreadsMetrics",
    "PoliticsNarrativeThreadsToken"
)
foreach ($Name in $Names) {
    $Task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $Task) {
        [pscustomobject]@{ TaskName = $Name; State = "NotRegistered"; LastResult = $null }
        continue
    }
    $Info = Get-ScheduledTaskInfo -TaskName $Name
    [pscustomobject]@{
        TaskName = $Name
        State = $Task.State
        LastRunTime = $Info.LastRunTime
        NextRunTime = $Info.NextRunTime
        LastResult = $Info.LastTaskResult
    }
}
