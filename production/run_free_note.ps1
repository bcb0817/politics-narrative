param(
    [ValidateSet(
        "weekly_top5",
        "legislative_process",
        "cabinet_decision_vs_law",
        "social_insurance_burden",
        "party_policy_comparison",
        "evergreen_institutional_explainer",
        "weekly_deep_dive"
    )]
    [string]$ArticleType = "weekly_top5"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Bot = Join-Path $Root "local_bot.py"
$LogDirectory = Join-Path $Root "logs"
$LogPath = Join-Path $LogDirectory ("free_note_{0}.log" -f $ArticleType)

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable not found: $Python"
}
if (-not (Test-Path -LiteralPath $Bot -PathType Leaf)) {
    throw "Bot entry point not found: $Bot"
}

Set-Location -LiteralPath $Root
New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
try {
    & $Python $Bot generate-free-note --type $ArticleType *>> $LogPath
    $BotExitCode = $LASTEXITCODE
}
catch {
    $ErrorSummary = "{0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message
    Add-Content -LiteralPath $LogPath -Value $ErrorSummary -Encoding UTF8
    exit 1
}
if ($BotExitCode -ne 0) {
    Add-Content -LiteralPath $LogPath `
        -Value ("Bot exit code: {0}" -f $BotExitCode) `
        -Encoding UTF8
    throw "Free note generation failed with exit code $BotExitCode"
}
exit 0
