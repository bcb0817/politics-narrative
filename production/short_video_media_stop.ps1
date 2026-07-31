$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pidPath = Join-Path $root "data\short_video_media_server.pid"
if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Host "Short-video media server is already stopped."
    exit 0
}
$serverPid = [int](Get-Content -LiteralPath $pidPath -Raw)
$running = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
if ($running) {
    Stop-Process -Id $serverPid
    Write-Host "Stopped short-video media server PID=$serverPid"
}
Remove-Item -LiteralPath $pidPath -Force
