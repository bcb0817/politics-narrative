# Legacy compatibility no-op.
# Daily review is integrated into PoliticsNarrativeBot. This file intentionally
# performs no API call and no X write even if the administrator-owned legacy
# scheduled task remains registered.
$Root = Split-Path -Parent $PSScriptRoot
$LogDirectory = Join-Path $Root "logs"
$LogFile = Join-Path $LogDirectory "daily_review.log"
New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$Line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [INFO] legacy daily-review task skipped; integrated into PoliticsNarrativeBot"
Add-Content -LiteralPath $LogFile -Value $Line -Encoding UTF8
exit 0
