$names = @(
    "PoliticsNarrativeCrosspostPrepare",
    "PoliticsNarrativeCrosspostPublish",
    "PoliticsNarrativeCrosspostReconcile",
    "PoliticsNarrativeCrosspostMetrics"
)

foreach ($name in $names) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        [pscustomobject]@{Task=$name; Registered=$true; State=$task.State}
    } else {
        [pscustomobject]@{Task=$name; Registered=$false; State="NotRegistered"}
    }
}

$root = Split-Path -Parent $PSScriptRoot
& (Join-Path $root ".venv\Scripts\python.exe") `
    (Join-Path $root "local_bot.py") crosspost-status
