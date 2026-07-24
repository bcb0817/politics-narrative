#Requires -Version 5.1
$ErrorActionPreference = "Stop"
$LegacyTaskName = "PoliticsNarrativeDailyReview"

$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
$IsAdministrator = $Principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

if (-not $IsAdministrator) {
    throw "Run this script from an elevated Windows PowerShell session."
}

$Task = Get-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue
if (-not $Task) {
    Write-Host "Legacy task is not registered." -ForegroundColor Green
    exit 0
}

Stop-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName $LegacyTaskName -ErrorAction Stop | Out-Null
$Verified = Get-ScheduledTask -TaskName $LegacyTaskName -ErrorAction Stop
Write-Host "Legacy task disabled: $($Verified.TaskName)" -ForegroundColor Green
