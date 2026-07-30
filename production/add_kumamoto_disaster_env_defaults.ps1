[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvPath = Join-Path $Root ".env"
if (-not (Test-Path -LiteralPath $EnvPath)) {
    throw ".env was not found: $EnvPath"
}

$Defaults = [ordered]@{
    KUMAMOTO_DISASTER_UPDATES_ENABLED = "true"
    KUMAMOTO_DISASTER_PHASE = "A"
    KUMAMOTO_DISASTER_PUBLISH_ENABLED = "false"
    KUMAMOTO_DISASTER_AUTO_POST_ENABLED = "false"
    KUMAMOTO_DISASTER_X_ENABLED = "true"
    KUMAMOTO_DISASTER_THREADS_ENABLED = "true"
    KUMAMOTO_DISASTER_X_POST_ENABLED = "false"
    KUMAMOTO_DISASTER_THREADS_POST_ENABLED = "false"
    KUMAMOTO_DISASTER_HUMAN_APPROVAL_REQUIRED = "true"
    KUMAMOTO_DISASTER_AUTO_PUBLISH_VERIFIED_ONLY = "true"
    KUMAMOTO_DISASTER_CLOSURE_SUMMARY_ENABLED = "true"
    KUMAMOTO_DISASTER_MORNING_CUTOFF = "07:00"
    KUMAMOTO_DISASTER_MORNING_PUBLISH = "07:30"
    KUMAMOTO_DISASTER_EVENING_CUTOFF = "19:00"
    KUMAMOTO_DISASTER_EVENING_PUBLISH = "19:30"
    KUMAMOTO_DISASTER_TIMEZONE = "Asia/Tokyo"
    KUMAMOTO_DISASTER_REQUIRE_OFFICIAL_SOURCE = "true"
    KUMAMOTO_DISASTER_REQUIRE_MEANINGFUL_CHANGE = "true"
    KUMAMOTO_DISASTER_MAX_SOURCE_AGE_HOURS = "12"
    KUMAMOTO_DISASTER_VISUAL_ENABLED = "true"
    KUMAMOTO_DISASTER_X_IMAGE_SIZE = "1600x900"
    KUMAMOTO_DISASTER_THREADS_IMAGE_SIZE = "1080x1350"
    KUMAMOTO_DISASTER_CORRECTION_ENABLED = "true"
    KUMAMOTO_DISASTER_CORRECTION_AUTO_POST = "false"
    KUMAMOTO_DISASTER_SHORT_CANDIDATE_ENABLED = "true"
    KUMAMOTO_DISASTER_SHORT_AUTO_PUBLISH = "false"
}

$Existing = Get-Content -LiteralPath $EnvPath -Encoding UTF8
$ExistingKeys = @{}
foreach ($Line in $Existing) {
    if ($Line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
        $ExistingKeys[$Matches[1]] = $true
    }
}

$Missing = @()
foreach ($Entry in $Defaults.GetEnumerator()) {
    if (-not $ExistingKeys.ContainsKey($Entry.Key)) {
        $Missing += "$($Entry.Key)=$($Entry.Value)"
    }
}

if ($Missing.Count -gt 0 -and
    $PSCmdlet.ShouldProcess($EnvPath, "Append missing Kumamoto disaster keys")) {
    Add-Content -LiteralPath $EnvPath -Encoding UTF8 -Value ""
    Add-Content -LiteralPath $EnvPath -Encoding UTF8 `
        -Value "# Kumamoto disaster updates / lifecycle safety defaults"
    Add-Content -LiteralPath $EnvPath -Encoding UTF8 -Value $Missing
}

[pscustomobject]@{
    EnvPath = $EnvPath
    ExistingKeysPreserved = $ExistingKeys.Count
    MissingKeysAdded = $Missing.Count
    AutoPostEnabled = $false
}
