$ErrorActionPreference = "Stop"
$TaskName = "PoliticsNarrativeBot"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Runner = Join-Path $PSScriptRoot "run_bot.ps1"

if (-not (Test-Path $Python)) { throw "Virtual environment not found. Run production\install.ps1 first." }
if (-not (Test-Path $Runner)) { throw "PowerShell runner not found: $Runner" }
if (-not (Test-Path (Join-Path $Root ".env"))) { throw ".env not found. Run production\install.ps1 first." }

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`"" `
    -WorkingDirectory $Root

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Politics news and commentary X bot with integrated daily review" `
    -Force | Out-Null

$Registered = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
if ($Registered.TaskName -ne $TaskName) {
    throw "Scheduled task registration could not be verified."
}

$LegacyTask = Get-ScheduledTask -TaskName "PoliticsNarrativeDailyReview" -ErrorAction SilentlyContinue
if ($LegacyTask) {
    Stop-ScheduledTask -TaskName "PoliticsNarrativeDailyReview" -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName "PoliticsNarrativeDailyReview" -ErrorAction SilentlyContinue | Out-Null
}

Write-Host "タスクを登録しました（未起動）: $TaskName" -ForegroundColor Green
$Registered | Format-List TaskName, State
