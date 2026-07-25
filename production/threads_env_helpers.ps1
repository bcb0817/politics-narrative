function Get-ThreadsEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$EnvFile,
        [Parameter(Mandatory = $true)][string]$Key
    )

    $Pattern = "^$([regex]::Escape($Key))=(.*)$"
    $Matches = @(
        [System.IO.File]::ReadAllLines(
            $EnvFile, [System.Text.Encoding]::UTF8
        ) | Where-Object { $_ -match $Pattern }
    )
    if ($Matches.Count -gt 1) {
        throw "Duplicate key in .env: $Key"
    }
    if ($Matches.Count -eq 0) {
        return $null
    }
    return ($Matches[0] -replace $Pattern, '$1').Trim().Trim('"').Trim("'")
}

function Set-ThreadsEnvValues {
    param(
        [Parameter(Mandatory = $true)][string]$EnvFile,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Updates
    )

    if (-not (Test-Path -LiteralPath $EnvFile)) {
        throw ".env not found: $EnvFile"
    }
    $Lines = [System.Collections.Generic.List[string]]::new()
    [System.IO.File]::ReadAllLines(
        $EnvFile, [System.Text.Encoding]::UTF8
    ) | ForEach-Object { [void]$Lines.Add($_) }

    foreach ($Key in $Updates.Keys) {
        $Indexes = @()
        for ($Index = 0; $Index -lt $Lines.Count; $Index++) {
            if ($Lines[$Index] -match "^$([regex]::Escape($Key))=") {
                $Indexes += $Index
            }
        }
        if ($Indexes.Count -gt 1) {
            throw "Duplicate key in .env: $Key"
        }
        $Replacement = "$Key=$($Updates[$Key])"
        if ($Indexes.Count -eq 1) {
            $Lines[$Indexes[0]] = $Replacement
        } else {
            [void]$Lines.Add($Replacement)
        }
    }

    [System.IO.File]::WriteAllLines(
        $EnvFile, $Lines, [System.Text.UTF8Encoding]::new($false))
}
