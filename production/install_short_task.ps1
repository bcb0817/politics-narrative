param([switch]$Enable)
$ErrorActionPreference = "Stop"
$shortRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$shortOld = Get-ScheduledTask -TaskName "PoliticsNarrativeBot"
if ($shortOld.State -eq 'Running') { throw 'Stop the existing task before changing its action.' }
$shortPython = (Get-Command python -ErrorAction Stop).Source
$shortScript = Join-Path $shortRoot 'production\run_short_posts.ps1'
$shortArguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Python "{1}"' -f $shortScript, $shortPython
$shortAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $shortArguments -WorkingDirectory $shortRoot
$shortTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 20)
$shortSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$shortTask = New-ScheduledTask -Action $shortAction -Trigger $shortTrigger -Settings $shortSettings -Principal $shortOld.Principal -Description 'RSS short news: target 10/day JST, safe gates, no forced posts; 20-minute checks.'
Register-ScheduledTask -TaskName 'PoliticsNarrativeBot' -InputObject $shortTask -Force | Out-Null
if (-not $Enable) { Disable-ScheduledTask -TaskName 'PoliticsNarrativeBot' | Out-Null }
Get-ScheduledTask -TaskName 'PoliticsNarrativeBot' | Select-Object TaskName, State
exit 0
