$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Bot = Join-Path $Root "local_bot.py"
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

if (Test-Path -LiteralPath $Python) {
    & $Python $Bot threads-status
}
