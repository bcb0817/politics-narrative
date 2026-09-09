[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$RepositoryRoot = ""
)

$ErrorActionPreference = "Stop"
if (-not $RepositoryRoot) {
    $RepositoryRoot = Split-Path -Parent $PSScriptRoot
}
$envPath = Join-Path $RepositoryRoot ".env"
$examplePath = Join-Path $RepositoryRoot ".env.example"

if (-not (Test-Path -LiteralPath $envPath)) {
    throw ".env was not found."
}
if (-not (Test-Path -LiteralPath $examplePath)) {
    throw ".env.example was not found."
}

$existingLines = @(Get-Content -LiteralPath $envPath -Encoding UTF8)
$existingKeys = @{}
foreach ($line in $existingLines) {
    if ($line -match "^([A-Z0-9_]+)=") {
        $existingKeys[$Matches[1]] = $true
    }
}

$missingLines = @()
foreach ($line in Get-Content -LiteralPath $examplePath -Encoding UTF8) {
    if ($line -match "^(THREADS_[A-Z0-9_]+)=") {
        $key = $Matches[1]
        if (-not $existingKeys.ContainsKey($key)) {
            $missingLines += $line
            $existingKeys[$key] = $true
        }
    }
}

if ($missingLines.Count -eq 0) {
    Write-Output "No missing THREADS_ keys."
    exit 0
}

if ($PSCmdlet.ShouldProcess($envPath, "Append missing THREADS_ defaults")) {
    $updated = @($existingLines)
    if ($updated.Count -gt 0 -and $updated[-1] -ne "") {
        $updated += ""
    }
    $updated += "# Threads full official API defaults (missing keys only)"
    $updated += $missingLines
    [System.IO.File]::WriteAllLines(
        $envPath,
        $updated,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

Write-Output ("Added missing THREADS_ keys: " + $missingLines.Count)
