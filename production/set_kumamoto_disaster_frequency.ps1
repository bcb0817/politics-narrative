[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "active_twice_daily",
        "active_daily",
        "recovery_periodic",
        "closed"
    )]
    [string]$Mode,

    [switch]$ConfirmChange
)

$ErrorActionPreference = "Stop"
$IncidentId = "kumamoto-earthquake-20260728"
$MorningTask = "PoliticsNarrativeKumamotoMorningUpdate"
$EveningTask = "PoliticsNarrativeKumamotoEveningUpdate"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Bot = Join-Path $Root "local_bot.py"

if (-not $ConfirmChange) {
    throw "Specify -ConfirmChange after human review. No mode or task was changed."
}

$Missing = @(
    $MorningTask,
    $EveningTask
) | Where-Object {
    $null -eq (Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue)
}
if ($Missing.Count -gt 0) {
    throw "Required tasks are not registered: $($Missing -join ', ')"
}

if (-not $PSCmdlet.ShouldProcess(
        "$MorningTask, $EveningTask",
        "Apply human-approved disaster frequency mode '$Mode'")) {
    return
}

& $Python $Bot disaster-frequency-apply `
    --incident-id $IncidentId `
    --mode $Mode `
    --confirm
if ($LASTEXITCODE -ne 0) {
    throw "The approved mode could not be recorded. Tasks were not changed."
}

switch ($Mode) {
    "active_twice_daily" {
        Set-ScheduledTask -TaskName $MorningTask `
            -Trigger (New-ScheduledTaskTrigger -Daily -At "07:00") | Out-Null
        Set-ScheduledTask -TaskName $EveningTask `
            -Trigger (New-ScheduledTaskTrigger -Daily -At "19:00") | Out-Null
        Enable-ScheduledTask -TaskName $MorningTask | Out-Null
        Enable-ScheduledTask -TaskName $EveningTask | Out-Null
    }
    "active_daily" {
        Set-ScheduledTask -TaskName $EveningTask `
            -Trigger (New-ScheduledTaskTrigger -Daily -At "19:00") | Out-Null
        Disable-ScheduledTask -TaskName $MorningTask | Out-Null
        Enable-ScheduledTask -TaskName $EveningTask | Out-Null
    }
    "recovery_periodic" {
        Set-ScheduledTask -TaskName $EveningTask `
            -Trigger (
                New-ScheduledTaskTrigger -Daily -DaysInterval 3 -At "19:00"
            ) | Out-Null
        Disable-ScheduledTask -TaskName $MorningTask | Out-Null
        Enable-ScheduledTask -TaskName $EveningTask | Out-Null
    }
    "closed" {
        Disable-ScheduledTask -TaskName $MorningTask | Out-Null
        Disable-ScheduledTask -TaskName $EveningTask | Out-Null
    }
}

Write-Output "Approved mode recorded: $Mode"
Write-Output "No task was started immediately."
