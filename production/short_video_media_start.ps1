$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

$pidPath = Join-Path $root "data\short_video_media_server.pid"
if (Test-Path -LiteralPath $pidPath) {
    $existingPid = [int](Get-Content -LiteralPath $pidPath -Raw)
    if (Get-Process -Id $existingPid -ErrorAction SilentlyContinue) {
        Write-Host "Short-video media server is already running. PID=$existingPid"
        exit 0
    }
}

$arguments = '"{0}" --host 127.0.0.1 --port 8766' -f (
    Join-Path $root "src\short_video_media_server.py")
$process = Start-Process -FilePath "$root\.venv\Scripts\python.exe" `
    -ArgumentList $arguments `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $pidPath) | Out-Null
[System.IO.File]::WriteAllText($pidPath, [string]$process.Id)
Write-Host "Short-video media server started. PID=$($process.Id)"
