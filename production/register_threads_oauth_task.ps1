param([switch]$Start)

$ErrorActionPreference = "Stop"
$TaskName = "PoliticsNarrativeThreadsOAuth"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Runner = Join-Path $PSScriptRoot "run_threads_oauth_server.ps1"
$EnvFile = Join-Path $Root ".env"
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment not found. Run production\install.ps1 first."
}
if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Threads OAuth runner not found: $Runner"
}
if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw ".env not found."
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`"" `
    -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Principal = New-ScheduledTaskPrincipal `
    -UserId $User `
    -LogonType Interactive `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description "Politics Narrative Bot - Meta Threads OAuth callback server" `
    -Force | Out-Null

if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
}

Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop |
    Select-Object TaskName, State
