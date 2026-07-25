param([switch]$Force)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_threads.ps1"
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -StartWhenAvailable -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)
$Principal = New-ScheduledTaskPrincipal -UserId $User `
    -LogonType Interactive -RunLevel Limited

function Register-ThreadsTask {
    param([string]$Name, [string]$Mode, [datetime[]]$Times)
    if ((Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) -and -not $Force) {
        throw "$Name already exists. Use -Force to replace it."
    }
    $Args = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`" -Mode $Mode"
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument $Args -WorkingDirectory $Root
    $Triggers = @($Times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ })
    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Triggers `
        -Principal $Principal -Settings $Settings `
        -Description "Politics Narrative Bot - official Meta Threads API ($Mode)" `
        -Force:$Force | Out-Null
}

Register-ThreadsTask "PoliticsNarrativeThreads" "scheduled" @(
    [datetime]"08:30", [datetime]"13:00", [datetime]"20:30")
Register-ThreadsTask "PoliticsNarrativeThreadsMetrics" "metrics" @(
    [datetime]"09:35", [datetime]"14:05", [datetime]"21:35")
Register-ThreadsTask "PoliticsNarrativeThreadsToken" "token" @([datetime]"03:30")

Write-Host "Threads tasks registered. Posting remains controlled by THREADS_POST_ENABLED."
