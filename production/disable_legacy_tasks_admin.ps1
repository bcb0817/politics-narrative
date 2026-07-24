$ErrorActionPreference = "Stop"

$CurrentTasks = @(
    "PoliticsNarrativeBot",
    "PoliticsNarrativeMetrics",
    "PoliticsNarrativeWeeklyReview"
)
$KnownLegacyTasks = @(
    "PoliticsNarrativeDailyReview",
    "PoliticsNarrativeBatchDailyReview",
    "PoliticsNarrativeReview"
)
$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "logs"
$LogFile = Join-Path $LogDir "disable_legacy_tasks_admin.log"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
$IsAdmin = $Principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsAdmin) {
    throw "Run this script from an elevated Administrator PowerShell."
}

$Candidates = Get-ScheduledTask | Where-Object {
    $Name = $_.TaskName
    $Arguments = [string]$_.Actions.Arguments
    ($KnownLegacyTasks -contains $Name) -or (
        $Name -like "*PoliticsNarrative*" -and
        $CurrentTasks -notcontains $Name -and
        ($Arguments -match "daily.review|04:45|batch.*daily")
    )
}

"[$(Get-Date -Format o)] Legacy task candidates:" |
    Tee-Object -FilePath $LogFile -Append
if (-not $Candidates) {
    "None" | Tee-Object -FilePath $LogFile -Append
} else {
    $Candidates | Select-Object TaskName, State |
        Format-Table -AutoSize | Out-String |
        Tee-Object -FilePath $LogFile -Append
}

foreach ($Task in $Candidates) {
    if ($CurrentTasks -contains $Task.TaskName) { continue }
    Disable-ScheduledTask -TaskName $Task.TaskName | Out-Null
    "Disabled: $($Task.TaskName)" | Tee-Object -FilePath $LogFile -Append
}

Write-Host "Verification:"
Write-Host "Get-ScheduledTask | Where-Object TaskName -like '*PoliticsNarrative*' | Select-Object TaskName,State"
Write-Host "Log: $LogFile"
