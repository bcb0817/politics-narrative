$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "=== politics-narrative 本番環境インストール ===" -ForegroundColor Cyan

# Prefer Python 3.12 for dependency compatibility.
& py -V:3.12 --version | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Host "Python 3.12をインストールしています..." -ForegroundColor Yellow
    & py install 3.12
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 installation failed." }
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & py -V:3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ".envを作成しました。APIキーを追加して保存し、install.ps1を再実行してください。" -ForegroundColor Yellow
    Start-Process notepad.exe -ArgumentList (Join-Path $Root ".env")
    exit 2
}

# Read required settings. Explicit UTF-8 avoids Windows PowerShell 5.1 encoding issues.
$EnvMap = @{}
[System.IO.File]::ReadAllLines((Join-Path $Root ".env"), [System.Text.Encoding]::UTF8) | ForEach-Object {
    $Line = $_.Trim()
    if ($Line -and -not $Line.StartsWith("#") -and $Line.Contains("=")) {
        $Parts = $Line.Split("=", 2)
        $EnvMap[$Parts[0].Trim()] = $Parts[1].Trim().Trim('"').Trim("'")
    }
}

$Required = @("OPENAI_API_KEY", "API_KEY", "API_KEY_SECRET", "ACCESS_TOKEN", "ACCESS_TOKEN_SECRET")
$Missing = @($Required | Where-Object { -not $EnvMap.ContainsKey($_) -or [string]::IsNullOrWhiteSpace($EnvMap[$_]) })
if ($Missing.Count -gt 0) {
    Write-Host "未設定の項目: $($Missing -join ', ')" -ForegroundColor Red
    Start-Process notepad.exe -ArgumentList (Join-Path $Root ".env")
    exit 3
}

# Smoke test with posting disabled only for this child process.
Write-Host "投稿を行わない簡易動作テストを実行しています..." -ForegroundColor Cyan
$OldPostEnabled = $env:POST_ENABLED
$env:POST_ENABLED = "false"
& $Python local_bot.py force
$SmokeCode = $LASTEXITCODE
if ($null -eq $OldPostEnabled) {
    Remove-Item Env:POST_ENABLED -ErrorAction SilentlyContinue
} else {
    $env:POST_ENABLED = $OldPostEnabled
}
if ($SmokeCode -ne 0) {
    Write-Host "簡易動作テストに失敗しました。logs\bot.log と logs\errors.jsonl を確認してください。" -ForegroundColor Red
    exit $SmokeCode
}

# Prevent old backlog slots from firing on the first production start.
& $Python local_bot.py init-state
if ($LASTEXITCODE -ne 0) { throw "init-state failed." }

Write-Host "インストールと簡易動作テストが完了しました。" -ForegroundColor Green
Write-Host "次の手順: powershell -ExecutionPolicy Bypass -File .\production\go_live.ps1" -ForegroundColor Cyan
