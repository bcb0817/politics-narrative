[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [ValidateSet("recommended", "latest")]
    [string]$Profile = "recommended",

    [string]$EnvPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($EnvPath)) {
    $EnvPath = Join-Path $ProjectRoot ".env"
}

$TargetPath = [System.IO.Path]::GetFullPath($EnvPath)
if (-not (Test-Path -LiteralPath $TargetPath -PathType Leaf)) {
    throw "Env file not found: $TargetPath"
}

$Profiles = @{
    recommended = [ordered]@{
        OPENAI_MODEL_DEFAULT                  = "gpt-5.4-nano"
        OPENAI_MODEL_IMPORTANT                = "gpt-5.4-mini"
        OPENAI_REASONING_EFFORT               = "none"
        OPENAI_DEFAULT_INPUT_USD_PER_1M       = "0.20"
        OPENAI_DEFAULT_CACHED_INPUT_USD_PER_1M = "0.02"
        OPENAI_DEFAULT_OUTPUT_USD_PER_1M      = "1.25"
        OPENAI_IMPORTANT_INPUT_USD_PER_1M     = "0.75"
        OPENAI_IMPORTANT_CACHED_INPUT_USD_PER_1M = "0.075"
        OPENAI_IMPORTANT_OUTPUT_USD_PER_1M    = "4.50"
    }
    latest = [ordered]@{
        OPENAI_MODEL_DEFAULT                  = "gpt-5.4-nano"
        OPENAI_MODEL_IMPORTANT                = "gpt-5.6-luna"
        OPENAI_REASONING_EFFORT               = "low"
        OPENAI_DEFAULT_INPUT_USD_PER_1M       = "0.20"
        OPENAI_DEFAULT_CACHED_INPUT_USD_PER_1M = "0.02"
        OPENAI_DEFAULT_OUTPUT_USD_PER_1M      = "1.25"
        OPENAI_IMPORTANT_INPUT_USD_PER_1M     = "1.00"
        OPENAI_IMPORTANT_CACHED_INPUT_USD_PER_1M = "0.10"
        OPENAI_IMPORTANT_OUTPUT_USD_PER_1M    = "6.00"
    }
}

$Settings = $Profiles[$Profile]
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupPath = "$TargetPath.backup.$Timestamp"
$TempPath = "$TargetPath.tmp.$PID"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

$Lines = [System.IO.File]::ReadAllLines($TargetPath, [System.Text.Encoding]::UTF8)
$Seen = @{}
$Updated = New-Object System.Collections.Generic.List[string]

foreach ($Line in $Lines) {
    if ($Line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
        $Key = $Matches[1]
        if ($Settings.Contains($Key)) {
            if ($Seen.ContainsKey($Key)) {
                continue
            }
            $Updated.Add("$Key=$($Settings[$Key])")
            $Seen[$Key] = $true
            continue
        }
    }
    $Updated.Add($Line)
}

$Missing = @($Settings.Keys | Where-Object { -not $Seen.ContainsKey($_) })
if ($Missing.Count -gt 0) {
    $Updated.Add("")
    $Updated.Add("# OpenAI model profile managed by production/update_openai_models.ps1")
    foreach ($Key in $Missing) {
        $Updated.Add("$Key=$($Settings[$Key])")
    }
}

if (-not $PSCmdlet.ShouldProcess($TargetPath, "Apply OpenAI model profile '$Profile'")) {
    return
}

Copy-Item -LiteralPath $TargetPath -Destination $BackupPath
try {
    [System.IO.File]::WriteAllLines($TempPath, $Updated, $Utf8NoBom)
    Move-Item -LiteralPath $TempPath -Destination $TargetPath -Force
}
catch {
    if (Test-Path -LiteralPath $TempPath) {
        Remove-Item -LiteralPath $TempPath -Force -ErrorAction SilentlyContinue
    }
    throw
}

Write-Host "OpenAI model profile updated: $Profile" -ForegroundColor Green
Write-Host "Env file: $TargetPath"
Write-Host "Backup: $BackupPath"
Write-Host "Updated keys:"
foreach ($Key in $Settings.Keys) {
    Write-Host "  - $Key"
}
Write-Host "Restart PoliticsNarrativeBot to load the new values." -ForegroundColor Yellow
