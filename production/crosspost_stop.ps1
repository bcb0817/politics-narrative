$names = @(
    "PoliticsNarrativeCrosspostPrepare",
    "PoliticsNarrativeCrosspostPublish",
    "PoliticsNarrativeCrosspostReconcile",
    "PoliticsNarrativeCrosspostMetrics"
)

foreach ($name in $names) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task -and $task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $name
    }
}

$root = Split-Path -Parent $PSScriptRoot
& (Join-Path $root ".venv\Scripts\python.exe") `
    (Join-Path $root "local_bot.py") crosspost-emergency-stop
