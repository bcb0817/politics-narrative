[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
$TaskNames = @(
    "PoliticsNarrativeKumamotoMorningUpdate",
    "PoliticsNarrativeKumamotoEveningUpdate"
)
foreach ($TaskName in $TaskNames) {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $Task -and
        $PSCmdlet.ShouldProcess($TaskName, "Enable scheduled task")) {
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
    }
}
Write-Output "Tasks enabled. No immediate run was started."
