[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvPath = Join-Path $Root ".env"
if (-not (Test-Path -LiteralPath $EnvPath)) {
    throw ".env was not found: $EnvPath"
}

$Required = [ordered]@{
    AUTONOMOUS_POSTING_ENABLED = "true"
    KUMAMOTO_DISASTER_UPDATES_ENABLED = "true"
    KUMAMOTO_DISASTER_PHASE = "C"
    KUMAMOTO_DISASTER_PUBLISH_ENABLED = "true"
    KUMAMOTO_DISASTER_AUTO_POST_ENABLED = "true"
    KUMAMOTO_DISASTER_X_ENABLED = "true"
    KUMAMOTO_DISASTER_THREADS_ENABLED = "true"
    KUMAMOTO_DISASTER_X_POST_ENABLED = "true"
    KUMAMOTO_DISASTER_THREADS_POST_ENABLED = "true"
    KUMAMOTO_DISASTER_HUMAN_APPROVAL_REQUIRED = "false"
    KUMAMOTO_DISASTER_AUTO_PUBLISH_VERIFIED_ONLY = "true"
    KUMAMOTO_DISASTER_CORRECTION_ENABLED = "true"
    KUMAMOTO_DISASTER_CORRECTION_AUTO_POST = "true"
}

$Lines = [System.Collections.Generic.List[string]]::new()
foreach ($Line in Get-Content -LiteralPath $EnvPath -Encoding UTF8) {
    $Lines.Add($Line)
}
$Changed = @()
foreach ($Entry in $Required.GetEnumerator()) {
    $Found = $false
    for ($Index = 0; $Index -lt $Lines.Count; $Index++) {
        if ($Lines[$Index] -match (
                '^\s*' + [regex]::Escape($Entry.Key) + '\s*=')) {
            $Found = $true
            $Expected = "$($Entry.Key)=$($Entry.Value)"
            if ($Lines[$Index] -ne $Expected) {
                $Lines[$Index] = $Expected
                $Changed += $Entry.Key
            }
            break
        }
    }
    if (-not $Found) {
        $Lines.Add("$($Entry.Key)=$($Entry.Value)")
        $Changed += $Entry.Key
    }
}

if ($Changed.Count -gt 0 -and $PSCmdlet.ShouldProcess(
        $EnvPath, "Enable autonomous verified Kumamoto disaster publishing")) {
    $Encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($EnvPath, $Lines, $Encoding)
}

[pscustomobject]@{
    Phase = "C"
    ChangedKeyCount = $Changed.Count
    DisasterPublishingEnabled = $true
    XEnabled = $true
    ThreadsEnabled = $true
    CorrectionAutoPostEnabled = $true
    SecretValuesPrinted = $false
}
