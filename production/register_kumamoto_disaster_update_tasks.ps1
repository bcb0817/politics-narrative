[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_kumamoto_disaster_update.ps1"
$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal `
    -UserId $UserId -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 25) -StartWhenAvailable

function New-DisasterAction {
    param([string]$SnapshotType)
    $Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
        '-WindowStyle Hidden -File "' + $Runner +
        '" -SnapshotType ' + $SnapshotType
    return New-ScheduledTaskAction `
        -Execute "powershell.exe" -Argument $Arguments -WorkingDirectory $Root
}

$Definitions = @(
    @{
        Name = "PoliticsNarrativeKumamotoMorningUpdate"
        Type = "morning"
        Trigger = New-ScheduledTaskTrigger -Daily -At "07:00"
    },
    @{
        Name = "PoliticsNarrativeKumamotoEveningUpdate"
        Type = "evening"
        Trigger = New-ScheduledTaskTrigger -Daily -At "19:00"
    }
)

foreach ($Definition in $Definitions) {
    if ($PSCmdlet.ShouldProcess($Definition.Name, "Register scheduled task")) {
        Register-ScheduledTask `
            -TaskName $Definition.Name `
            -Action (New-DisasterAction -SnapshotType $Definition.Type) `
            -Trigger $Definition.Trigger `
            -Principal $Principal `
            -Settings $Settings `
            -Description "Kumamoto earthquake Phase A snapshot and candidate generation" `
            -Force | Out-Null
    }
}

Write-Output "Registration completed. Tasks were not started."
