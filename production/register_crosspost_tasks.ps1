param([switch]$WhatIf)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$bot = Join-Path $root "local_bot.py"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found: $python"
}

$specs = @(
    @{Name="PoliticsNarrativeCrosspostPrepare"; At="18:30"; Command="crosspost-prepare --dry-run"},
    @{Name="PoliticsNarrativeCrosspostPublish"; At="20:00"; Command="crosspost-publish --dry-run"},
    @{Name="PoliticsNarrativeCrosspostReconcile"; At="20:10"; Command="crosspost-reconcile --dry-run"},
    @{Name="PoliticsNarrativeCrosspostMetrics"; At="00:15"; Command="crosspost-metrics-sync --dry-run"; RepeatHours=6}
)

foreach ($spec in $specs) {
    if ($WhatIf) {
        [pscustomobject]@{Task=$spec.Name; At=$spec.At; Command=$spec.Command; Registered=$false}
        continue
    }
    $arguments = "`"$bot`" $($spec.Command)"
    $action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $root
    if ($spec.RepeatHours) {
        $trigger = New-ScheduledTaskTrigger -Once -At $spec.At `
            -RepetitionInterval (New-TimeSpan -Hours $spec.RepeatHours)
    } else {
        $trigger = New-ScheduledTaskTrigger -Daily -At $spec.At
    }
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable
    Register-ScheduledTask -TaskName $spec.Name -Action $action -Trigger $trigger `
        -Settings $settings -Description "Politics Narrative Phase A crosspost task" -Force | Out-Null
}

Write-Output "Crosspost task registration completed. Tasks are not started by this script."
