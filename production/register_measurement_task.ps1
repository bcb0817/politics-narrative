param(
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_measurement_cycle.ps1"
$TaskName = "PoliticsNarrativeMeasurementCycle"

if (-not $Apply) {
    [pscustomobject]@{
        Status = "WhatIf"
        Registered = 0
        TaskName = $TaskName
        IntervalMinutes = 15
        Command = "$Runner -Execute"
        Message = "No task was registered. Review status, scopes, and budget first."
    }
    exit 0
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Runner`" -Execute"
) -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description (
        "Read-only X metrics, replies, and follower snapshots; no social writes"
    ) -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName,State
