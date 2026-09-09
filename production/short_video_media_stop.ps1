$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pidPath = Join-Path $root "data\short_video_media_server.pid"
if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Host "Short-video media server is already stopped."
    exit 0
}
$pidText = (Get-Content -LiteralPath $pidPath -Raw).Trim()
$parsedPid = 0
if (-not [int]::TryParse($pidText, [ref]$parsedPid) -or $parsedPid -le 0) {
    Write-Warning "Invalid short-video media server PID file. Removing it."
    Remove-Item -LiteralPath $pidPath -Force
    exit 0
}
$running = Get-Process -Id $parsedPid -ErrorAction SilentlyContinue
if ($null -ne $running) {
    $running | Stop-Process -Force
    Write-Host "Stopped short-video media server PID=$parsedPid"
}
Remove-Item -LiteralPath $pidPath -Force
