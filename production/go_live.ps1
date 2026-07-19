$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
Set-Location $Root

if (-not (Test-Path $EnvFile)) { throw ".env was not found." }
if (-not (Test-Path $Python)) { throw ".venv was not found. Run production\install.ps1 first." }

$Text = [System.IO.File]::ReadAllText($EnvFile, [System.Text.Encoding]::UTF8)
if ($Text -notmatch '(?m)^POST_ENABLED\s*=\s*true\s*$') {
    Write-Host "POST_ENABLED が true ではありません。.env を開きます。" -ForegroundColor Yellow
    Start-Process notepad.exe -ArgumentList $EnvFile
    exit 2
}

& $Python local_bot.py init-state
if ($LASTEXITCODE -ne 0) { throw "init-state failed." }
& (Join-Path $PSScriptRoot "register_task.ps1")
if ($LASTEXITCODE -ne 0) { throw "Scheduled task registration failed." }
Write-Host "本番モードを開始しました。日次レビューはBot本体が自動実行します。" -ForegroundColor Green
& $Python local_bot.py status
