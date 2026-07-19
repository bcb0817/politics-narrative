$ErrorActionPreference = "Stop"
$TaskName = "PoliticsNarrativeBot"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $Python)) { throw "Virtual environment not found. Run production\install.ps1 first." }
if (-not (Test-Path $Pythonw)) { throw "pythonw.exe was not found in the virtual environment." }
if (-not (Test-Path (Join-Path $Root ".env"))) { throw ".env not found. Run production\install.ps1 first." }

$Action = New-ScheduledTaskAction `
    -Execute $Pythonw `
    -Argument "`"$(Join-Path $Root 'local_bot.py')`" daemon" `
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

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
Write-Host "タスクを登録して開始しました: $TaskName" -ForegroundColor Green
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State
