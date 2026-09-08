$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$SupervisorLog = Join-Path $Root "logs\supervisor.log"

Set-Location $Root
New-Item -ItemType Directory -Path (Join-Path $Root "logs") -Force | Out-Null

function Write-SupervisorLog([string]$Message) {
    $Line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $SupervisorLog -Value $Line -Encoding UTF8
}

if (-not (Test-Path $Python)) {
    Write-SupervisorLog "[エラー] 仮想環境が見つかりません: $Python"
    exit 1
}

Write-SupervisorLog "[情報] 監視処理を開始しました。"
while ($true) {
    try {
        & $Python (Join-Path $Root "local_bot.py") daemon
        $Code = $LASTEXITCODE
        Write-SupervisorLog "[警告] デーモンが終了しました（終了コード=$Code）。60秒後に再起動します。"
    } catch {
        Write-SupervisorLog "[エラー] デーモンが異常終了しました: $($_.Exception.Message)。60秒後に再起動します。"
    }
    Start-Sleep -Seconds 60
}
