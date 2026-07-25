$ErrorActionPreference = "Stop"
$TaskName = "PoliticsNarrativeThreadsOAuth"
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if (-not $Task) {
    Write-Host "Threads OAuth task is not registered."
    exit 1
}

$Info = Get-ScheduledTaskInfo -TaskName $TaskName
[pscustomobject]@{
    TaskName       = $Task.TaskName
    State          = $Task.State
    LastRunTime    = $Info.LastRunTime
    LastTaskResult = $Info.LastTaskResult
    NextRunTime    = $Info.NextRunTime
} | Format-List
