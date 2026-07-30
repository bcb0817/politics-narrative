[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
$TaskNames = @(
    "PoliticsNarrativeKumamotoMorningUpdate",
    "PoliticsNarrativeKumamotoEveningUpdate"
)
foreach ($TaskName in $TaskNames) {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $Task) {
        if ($Task.State -eq "Running" -and
            $PSCmdlet.ShouldProcess($TaskName, "Stop scheduled task")) {
            Stop-ScheduledTask -TaskName $TaskName
        }
        if ($PSCmdlet.ShouldProcess($TaskName, "Disable scheduled task")) {
            Disable-ScheduledTask -TaskName $TaskName | Out-Null
        }
    }
}
Write-Output "Tasks stopped and disabled."
