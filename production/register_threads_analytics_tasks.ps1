[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot "run_threads_analytics.ps1"
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -StartWhenAvailable

function New-AnalyticsAction {
    param([string]$Mode)
    $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
        '-WindowStyle Hidden -File "' + $runner + '" -Mode ' + $Mode
    return New-ScheduledTaskAction `
        -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $root
}

$hourlyStart = (Get-Date).Date.AddHours((Get-Date).Hour + 1)
$definitions = @(
    @{
        Name = "PoliticsNarrativeThreadsSync"
        Mode = "sync"
        Trigger = New-ScheduledTaskTrigger -Once -At $hourlyStart `
            -RepetitionInterval (New-TimeSpan -Hours 1)
    },
    @{
        Name = "PoliticsNarrativeThreadsInsights"
        Mode = "insights"
        Trigger = New-ScheduledTaskTrigger -Once -At $hourlyStart `
            -RepetitionInterval (New-TimeSpan -Hours 1)
    },
    @{
        Name = "PoliticsNarrativeThreadsSearch"
        Mode = "search"
        Trigger = New-ScheduledTaskTrigger -Once -At $hourlyStart `
            -RepetitionInterval (New-TimeSpan -Hours 1)
    },
    @{
        Name = "PoliticsNarrativeThreadsDailyReport"
        Mode = "daily"
        Trigger = New-ScheduledTaskTrigger -Daily -At "23:30"
    },
    @{
        Name = "PoliticsNarrativeThreadsWeeklyReport"
        Mode = "weekly"
        Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "22:00"
    },
    @{
        Name = "PoliticsNarrativeThreadsTokenRefresh"
        Mode = "token"
        Trigger = New-ScheduledTaskTrigger -Daily -At "03:30"
    }
)

foreach ($definition in $definitions) {
    if ($PSCmdlet.ShouldProcess($definition.Name, "Register scheduled task")) {
        Register-ScheduledTask `
            -TaskName $definition.Name `
            -Action (New-AnalyticsAction -Mode $definition.Mode) `
            -Trigger $definition.Trigger `
            -Principal $principal `
            -Settings $settings `
            -Description "Read-first official Threads API analytics" `
            -Force | Out-Null
    }
}

Write-Output "Registration script completed. Tasks were not started."
