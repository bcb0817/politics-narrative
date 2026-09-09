param([switch]$WhatIf)

$names = @(
    "PoliticsNarrativeCrosspostPrepare",
    "PoliticsNarrativeCrosspostPublish",
    "PoliticsNarrativeCrosspostReconcile",
    "PoliticsNarrativeCrosspostMetrics"
)

foreach ($name in $names) {
    if ($WhatIf) {
        Write-Output "Would start: $name"
        continue
    }
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        Start-ScheduledTask -TaskName $name
    } else {
        Write-Warning "Not registered: $name"
    }
}
