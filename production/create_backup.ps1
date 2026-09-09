param(
    [string]$Label = "",
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$backupRoot = Join-Path $root "backups"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$safeLabel = ($Label -replace "[^A-Za-z0-9_-]", "-").Trim("-")
$name = if ($safeLabel) {
    "politics-narrative-backup-$safeLabel-$stamp"
} else {
    "politics-narrative-backup-$stamp"
}
$destination = Join-Path $backupRoot $name

if (-not $destination.StartsWith(
    $backupRoot + [IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "Unsafe backup destination: $destination"
}

if ($WhatIf) {
    [pscustomobject]@{
        Source = $root
        Destination = $destination
        Excludes = "backups, archive, .git, .venv, __pycache__, outputs, logs, data/daemon.lock"
        Created = $false
    }
    exit 0
}

New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
New-Item -ItemType Directory -Path $destination -Force | Out-Null

& robocopy $root $destination /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 `
    /XD (Join-Path $root "backups") `
        (Join-Path $root "archive") `
        (Join-Path $root ".git") `
        (Join-Path $root ".venv") `
        (Join-Path $root "__pycache__") `
        (Join-Path $root "outputs") `
        (Join-Path $root "logs") `
    /XF (Join-Path $root "data\daemon.lock") `
    /NFL /NDL /NJH /NJS /NP | Out-Null

$robocopyCode = $LASTEXITCODE
if ($robocopyCode -gt 7) {
    throw "Backup failed with robocopy exit code $robocopyCode"
}

$files = (Get-ChildItem -LiteralPath $destination -Recurse -File |
    Measure-Object).Count
$bytes = (Get-ChildItem -LiteralPath $destination -Recurse -File |
    Measure-Object Length -Sum).Sum

[pscustomobject]@{
    Source = $root
    Destination = $destination
    Files = $files
    Megabytes = [math]::Round($bytes / 1MB, 2)
    Created = $true
}
