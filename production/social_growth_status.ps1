$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Bot = Join-Path $Root "local_bot.py"
$names = @(
    "PoliticsNarrativeContentInventory",
    "PoliticsNarrativeGrowthMetrics",
    "PoliticsNarrativeShortPromotion",
    "PoliticsNarrativeGrowthDailyReport",
    "PoliticsNarrativeGrowthWeeklyReport"
)

foreach ($name in $names) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) {
        [pscustomobject]@{ TaskName = $name; State = "NotRegistered"; NextRun = $null }
        continue
    }
    $info = Get-ScheduledTaskInfo -TaskName $name
    [pscustomobject]@{
        TaskName = $name
        State = [string]$task.State
        NextRun = $info.NextRunTime
        LastResult = $info.LastTaskResult
    }
}

if (Test-Path -LiteralPath $Python) {
    & $Python $Bot growth-status
}
