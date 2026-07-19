$ErrorActionPreference = "Stop"
$TaskName = "PoliticsNarrativeDailyReview"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_daily_review.ps1"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$EnvFile = Join-Path $Root ".env"

if (-not (Test-Path $Python)) { throw "Virtual environment not found. Run production\install.ps1 first." }
if (-not (Test-Path $EnvFile)) { throw ".env not found." }

$ReviewAt = "04:45"
$Line = Get-Content $EnvFile | Where-Object { $_ -match '^\s*DAILY_REVIEW_AT\s*=' } | Select-Object -Last 1
if ($Line) {
    $Candidate = ($Line -split '=', 2)[1].Trim()
    if ($Candidate -match '^([01]\d|2[0-3]):[0-5]\d$') { $ReviewAt = $Candidate }
}
$At = [DateTime]::ParseExact($ReviewAt, "HH:mm", [Globalization.CultureInfo]::InvariantCulture)

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Runner`"" `
    -WorkingDirectory $Root

$Trigger = New-ScheduledTaskTrigger -Daily -At $At
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
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
    -Description "Daily review of the previous 24 hours and top-3 learning data" `
    -Force | Out-Null

Write-Host "日次レビュータスクを登録しました: $TaskName（実行時刻: $ReviewAt）" -ForegroundColor Green
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State
