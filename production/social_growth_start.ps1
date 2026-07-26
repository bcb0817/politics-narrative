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
    if ($task) {
        Start-ScheduledTask -TaskName $name
        [pscustomobject]@{ TaskName = $name; Result = "Started" }
    } else {
        [pscustomobject]@{ TaskName = $name; Result = "NotRegistered" }
    }
}
