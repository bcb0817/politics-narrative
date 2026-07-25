$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Bot = Join-Path $Root "local_bot.py"
Set-Location -LiteralPath $Root

& $Python $Bot threads-web
if ($LASTEXITCODE -ne 0) {
    throw "Threads OAuth callback server exited with code $LASTEXITCODE"
}
