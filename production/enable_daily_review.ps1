$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
Set-Location $Root

if (-not (Test-Path $EnvFile)) { throw ".env not found." }
if (-not (Test-Path $Python)) { throw ".venv not found. Run production\install.ps1 first." }

$Text = [System.IO.File]::ReadAllText($EnvFile, [System.Text.Encoding]::UTF8)
$Settings = [ordered]@{
    "DAILY_REVIEW_AT" = "04:45"
    "DAILY_REVIEW_WINDOW_HOURS" = "24"
}
foreach ($Name in $Settings.Keys) {
    $Value = $Settings[$Name]
    $Pattern = "(?m)^\s*" + [regex]::Escape($Name) + "\s*=.*$"
    $Line = "$Name=$Value"
    if ([regex]::IsMatch($Text, $Pattern)) {
        $Text = [regex]::Replace($Text, $Pattern, $Line)
    } else {
        if (-not $Text.EndsWith("`r`n")) { $Text += "`r`n" }
        $Text += "$Line`r`n"
    }
}
$Utf8Bom = New-Object System.Text.UTF8Encoding($true)
[System.IO.File]::WriteAllText($EnvFile, $Text, $Utf8Bom)

& (Join-Path $PSScriptRoot "register_review_task.ps1")
if ($LASTEXITCODE -ne 0) { throw "Daily review task registration failed." }

Write-Host "日次レビューを有効にしました。次のコマンドで動作確認してください:" -ForegroundColor Green
Write-Host ".\.venv\Scripts\python.exe local_bot.py report" -ForegroundColor Cyan
