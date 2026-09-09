param(
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$bot = Join-Path $root "local_bot.py"
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python runtime not found: $python"
}

function New-BotTask {
    param(
        [string]$Name,
        [string]$Arguments,
        [datetime]$StartAt,
        [timespan]$Interval
    )
    if ($WhatIf) {
        [pscustomobject]@{
            TaskName = $Name
            Execute = $python
            Arguments = "`"$bot`" $Arguments"
            StartAt = $StartAt
            Interval = $Interval
        }
        return
    }
    $action = New-ScheduledTaskAction `
        -Execute $python `
        -Argument "`"$bot`" $Arguments" `
        -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger `
        -Once -At $StartAt `
        -RepetitionInterval $Interval `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $principal = New-ScheduledTaskPrincipal `
        -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 2) `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "Politics Narrative Short Video Factory" `
        -Force | Out-Null
    Get-ScheduledTask -TaskName $Name |
        Select-Object TaskName,State
}

$now = Get-Date
New-BotTask `
    -Name "PoliticsNarrativeShortVideoFactory" `
    -Arguments "short-video-scheduled-run" `
    -StartAt $now.AddMinutes(2) `
    -Interval (New-TimeSpan -Hours 2)
New-BotTask `
    -Name "PoliticsNarrativeShortVideoQueue" `
    -Arguments "short-video-queue-run --live --limit 10" `
    -StartAt $now.AddMinutes(1) `
    -Interval (New-TimeSpan -Minutes 15)
