$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_free_note.ps1"

if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
    throw "Runner not found: $Runner"
}

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5)

$Definitions = @(
    @{
        Name = "PoliticsNarrativeFreeNoteWed"
        Day = "Wednesday"
        Time = "20:30"
        Type = "evergreen_institutional_explainer"
    },
    @{
        Name = "PoliticsNarrativeFreeNoteSun"
        Day = "Sunday"
        Time = "20:30"
        Type = "weekly_top5"
    }
)

foreach ($Definition in $Definitions) {
    $Arguments = "-NoProfile -NonInteractive -WindowStyle Hidden " +
        "-ExecutionPolicy Bypass -File `"$Runner`" " +
        "-ArticleType `"$($Definition.Type)`""
    $Action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument $Arguments `
        -WorkingDirectory $Root
    $Trigger = New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek $Definition.Day `
        -At $Definition.Time
    Register-ScheduledTask `
        -TaskName $Definition.Name `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Description "Generate a local free note draft and notify Discord for human review" `
        -Force | Out-Null
    $Registered = Get-ScheduledTask `
        -TaskName $Definition.Name `
        -ErrorAction Stop
    if (-not $Registered) {
        throw "Task registration failed: $($Definition.Name)"
    }
}

Get-ScheduledTask -TaskName "PoliticsNarrativeFreeNote*" |
    Select-Object TaskName, State,
        @{Name = "Execute"; Expression = {$_.Actions[0].Execute}},
        @{Name = "Arguments"; Expression = {$_.Actions[0].Arguments}} |
    Format-List
