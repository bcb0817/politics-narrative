$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "register_review_task.ps1")
if (-not (Get-ScheduledTask -TaskName "PoliticsNarrativeDailyReview" -ErrorAction SilentlyContinue)) { throw "Task registration failed" }
