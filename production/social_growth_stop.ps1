$ErrorActionPreference = "Stop"
$names = @(
    "PoliticsNarrativeContentInventory",
    "PoliticsNarrativeGrowthMetrics",
    "PoliticsNarrativeShortPromotion",
    "PoliticsNarrativeGrowthDailyReport",
    "PoliticsNarrativeGrowthWeeklyReport"
)

foreach ($name in $names) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task -and $task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $name
        [pscustomobject]@{ TaskName = $name; Result = "Stopped" }
    } elseif ($task) {
        [pscustomobject]@{ TaskName = $name; Result = [string]$task.State }
    } else {
        [pscustomobject]@{ TaskName = $name; Result = "NotRegistered" }
    }
}
