param([switch]$Apply)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_daily_post_goal.ps1"
$TaskName = "PoliticsNarrativeDailyPostGoal"

if (-not $Apply) {
    [pscustomobject]@{
        Status = "WhatIf"
        Registered = 0
        TaskName = $TaskName
        DailyAt = "18:00"
        Command = "$Runner -Save -Notify -ApplyRemediation"
        Message = "No task was registered or started."
    }
    exit 0
}

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Runner`" -Save -Notify -ApplyRemediation"
) -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Daily -At "18:00"
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "Daily 20-post goal progress monitoring and safe remediation; no direct social writes" `
    -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName,State
