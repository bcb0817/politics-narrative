$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$LogDir = Join-Path $Root "logs"
$ReviewLog = Join-Path $LogDir "daily_review.log"

Set-Location $Root
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Write-ReviewLog([string]$Message) {
    $Line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $ReviewLog -Value $Line -Encoding UTF8
}

if (-not (Test-Path $Python)) {
    Write-ReviewLog "[エラー] 仮想環境が見つかりません: $Python"
    exit 1
}

Write-ReviewLog "[情報] 日次レビューを開始しました。"
try {
    & $Python (Join-Path $Root "local_bot.py") report 2>&1 | ForEach-Object {
        Add-Content -Path $ReviewLog -Value $_ -Encoding UTF8
    }
    $Code = $LASTEXITCODE
    Write-ReviewLog "[情報] 日次レビューが終了しました（終了コード=$Code）。"
    exit $Code
} catch {
    Write-ReviewLog "[エラー] 日次レビューが異常終了しました: $($_.Exception.Message)"
    exit 1
}
