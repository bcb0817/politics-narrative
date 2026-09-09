[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateRange(1, 24)]
    [int]$Target = 20,
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $repoRoot ".env"

if (-not (Test-Path -LiteralPath $envPath)) {
    throw ".env was not found at $envPath"
}

$settings = [ordered]@{
    DAILY_POST_TARGET = $Target
    ORIGINAL_DAILY_POST_MIN = $Target
    ORIGINAL_DAILY_POST_MAX = $Target
    NORMAL_DAILY_POST_MAX = $Target
    MAX_DAILY_AUTOMATED_POSTS = $Target
    MAX_DAILY_POSTS = $Target
    X_POST_CREATE_MAX_PER_DAY = $Target
    X_POST_CREATE_MAX_PER_MONTH = ($Target * 30)
    EVERGREEN_MAX_PER_DAY = 2
}

if ($Target -ge 20) {
    $settings.MONITOR_INTERVAL_MINUTES = 45
    $settings.SLOT_INTERVAL_MINUTES = 45
    $settings.MIN_POST_INTERVAL_MINUTES = 45
}

Write-Host "Daily post target configuration preview:"
$settings.GetEnumerator() | Format-Table -AutoSize

if (-not $Apply) {
    Write-Host "No file was changed. Re-run with -Apply after creating a repository backup."
    exit 0
}

if (-not $PSCmdlet.ShouldProcess($envPath, "Set daily post target to $Target")) {
    exit 0
}

$content = Get-Content -LiteralPath $envPath -Raw -Encoding UTF8
foreach ($entry in $settings.GetEnumerator()) {
    $escapedName = [regex]::Escape([string]$entry.Key)
    $line = "{0}={1}" -f $entry.Key, $entry.Value
    if ($content -match "(?m)^$escapedName=") {
        $content = [regex]::Replace(
            $content,
            "(?m)^$escapedName=.*$",
            $line
        )
    }
    else {
        $content = $content.TrimEnd("`r", "`n") + "`r`n$line`r`n"
    }
}

$temporaryPath = "$envPath.daily-target.tmp"
[System.IO.File]::WriteAllText(
    $temporaryPath,
    $content,
    [System.Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $temporaryPath -Destination $envPath -Force
Write-Host "Applied. Restart the Bot manually for module-level settings to reload."
