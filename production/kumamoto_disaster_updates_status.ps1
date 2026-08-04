[CmdletBinding()]
param()

$TaskNames = @(
    "PoliticsNarrativeKumamotoMorningUpdate",
    "PoliticsNarrativeKumamotoEveningUpdate"
)
foreach ($TaskName in $TaskNames) {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $Task) {
        [pscustomobject]@{
            TaskName = $TaskName
            State = "NotRegistered"
            LastRunTime = $null
            NextRunTime = $null
            LastTaskResult = $null
        }
        continue
    }
    $Info = Get-ScheduledTaskInfo -TaskName $TaskName
    [pscustomobject]@{
        TaskName = $TaskName
        State = $Task.State
        LastRunTime = $Info.LastRunTime
        NextRunTime = $Info.NextRunTime
        LastTaskResult = $Info.LastTaskResult
    }
}
