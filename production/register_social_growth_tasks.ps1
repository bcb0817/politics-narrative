param(
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Bot = Join-Path $Root "local_bot.py"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python virtual environment was not found: $Python"
}

$definitions = @(
    @{ Name = "PoliticsNarrativeContentInventory"; At = "05:10"; Args = "`"$Bot`" growth-full-cycle --dry-run" },
    @{ Name = "PoliticsNarrativeGrowthMetrics"; At = "12:15"; Args = "`"$Bot`" growth-daily-report --dry-run" },
    @{ Name = "PoliticsNarrativeShortPromotion"; At = "18:15"; Args = "`"$Bot`" short-candidates --dry-run" },
    @{ Name = "PoliticsNarrativeGrowthDailyReport"; At = "23:45"; Args = "`"$Bot`" growth-daily-report --dry-run" },
    @{ Name = "PoliticsNarrativeGrowthWeeklyReport"; At = "04:20"; Args = "`"$Bot`" growth-weekly-report --dry-run" }
)

if (-not $Apply) {
    [pscustomobject]@{
        Status = "WhatIf"
        Registered = 0
        Message = "No task was registered. Re-run with -Apply after Phase B approval."
        Tasks = $definitions
    }
    exit 0
}

foreach ($definition in $definitions) {
    $action = New-ScheduledTaskAction `
        -Execute $Python `
        -Argument $definition.Args `
        -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -Daily -At $definition.At
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask `
        -TaskName $definition.Name `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description "PoliticsNarrative Phase A local candidate production; no external publish" `
        -Force | Out-Null
}

[pscustomobject]@{
    Status = "Registered"
    Registered = $definitions.Count
    Tasks = $definitions.Name
}
