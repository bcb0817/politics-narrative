param([switch]$Apply)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path -LiteralPath $EnvFile)) { throw ".env not found: $EnvFile" }

$Updates = [ordered]@{
    OPENAI_MODEL_CLASSIFIER = "gpt-5.4-nano"
    OPENAI_MODEL_DEFAULT = "gpt-5.4-mini"
    OPENAI_MODEL_POST = "gpt-5.4-mini"
    OPENAI_MODEL_IMPORTANT = "gpt-5.6-luna"
    OPENAI_MODEL_DAILY_REVIEW = "gpt-5.4-mini"
    OPENAI_MODEL_WEEKLY_REVIEW = "gpt-5.6-terra"
    OPENAI_MODEL_WEEKLY_REPORT = "gpt-5.6-terra"
    OPENAI_MODEL_PREMIUM = "gpt-5.6-sol"
}

$Lines = [System.Collections.Generic.List[string]]::new()
[System.IO.File]::ReadAllLines($EnvFile, [System.Text.Encoding]::UTF8) |
    ForEach-Object { [void]$Lines.Add($_) }

foreach ($Key in $Updates.Keys) {
    $Replacement = "$Key=$($Updates[$Key])"
    $Matches = @()
    for ($Index = 0; $Index -lt $Lines.Count; $Index++) {
        if ($Lines[$Index] -match "^$([regex]::Escape($Key))=") { $Matches += $Index }
    }
    if ($Matches.Count -gt 1) { throw "Duplicate key in .env: $Key" }
    if ($Apply -and $Matches.Count -eq 1) { $Lines[$Matches[0]] = $Replacement }
    if ($Apply -and $Matches.Count -eq 0) { [void]$Lines.Add($Replacement) }
    Write-Host "$Key -> $($Updates[$Key])"
}

if (-not $Apply) {
    Write-Host "Preview only. Re-run with -Apply to update only the keys above."
    exit 0
}

$Backup = "$EnvFile.models-$(Get-Date -Format 'yyyyMMdd-HHmmss').backup"
Copy-Item -LiteralPath $EnvFile -Destination $Backup
[System.IO.File]::WriteAllLines(
    $EnvFile, $Lines, [System.Text.UTF8Encoding]::new($false))
Write-Host "Updated: $EnvFile"
Write-Host "Backup: $Backup"

