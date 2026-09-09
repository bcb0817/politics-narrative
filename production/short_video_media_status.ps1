$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pidPath = Join-Path $root "data\short_video_media_server.pid"
if (Test-Path -LiteralPath $pidPath) {
    $serverPid = [int](Get-Content -LiteralPath $pidPath -Raw)
    $running = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
}
if ($running) {
    $running | Select-Object Id, ProcessName, StartTime
    exit 0
}
Write-Host "Short-video media server is not running."
exit 1
