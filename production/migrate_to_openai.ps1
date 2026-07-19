$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"
Set-Location $Root

if (-not (Test-Path $EnvFile)) {
    Copy-Item ".env.example" ".env"
}

$Text = [System.IO.File]::ReadAllText($EnvFile, [System.Text.Encoding]::UTF8)
$Settings = [ordered]@{
    "OPENAI_API_KEY" = ""
    "OPENAI_MODEL_DEFAULT" = "gpt-5-nano"
    "OPENAI_MODEL_IMPORTANT" = "gpt-5-mini"
    "OPENAI_REASONING_EFFORT" = "minimal"
    "OPENAI_MAX_OUTPUT_TOKENS" = "2400"
    "DAILY_IMPORTANT_MODEL_LIMIT" = "8"
    "OPENAI_MONTHLY_BUDGET_USD" = "8.0"
    "OPENAI_TIMEOUT_SECONDS" = "90"
    "OPENAI_DEFAULT_INPUT_USD_PER_1M" = "0.05"
    "OPENAI_DEFAULT_CACHED_INPUT_USD_PER_1M" = "0.005"
    "OPENAI_DEFAULT_OUTPUT_USD_PER_1M" = "0.40"
    "OPENAI_IMPORTANT_INPUT_USD_PER_1M" = "0.25"
    "OPENAI_IMPORTANT_CACHED_INPUT_USD_PER_1M" = "0.025"
    "OPENAI_IMPORTANT_OUTPUT_USD_PER_1M" = "2.00"
    "PREFILTER_TOP_N" = "1"
    "CANDIDATES_PER_NEWS" = "1"
    "THREAD_ENABLED" = "false"
}

foreach ($Name in $Settings.Keys) {
    $Value = $Settings[$Name]
    $Pattern = "(?m)^\s*" + [regex]::Escape($Name) + "\s*=.*$"
    if ([regex]::IsMatch($Text, $Pattern)) {
        if ($Name -ne "OPENAI_API_KEY" -or [regex]::Match($Text, $Pattern).Value -match "=\s*$") {
            $Text = [regex]::Replace($Text, $Pattern, "$Name=$Value")
        }
    } else {
        if (-not $Text.EndsWith("`r`n")) { $Text += "`r`n" }
        $Text += "$Name=$Value`r`n"
    }
}

# Keep the old Anthropic key only as a comment so rollback remains possible.
$Text = [regex]::Replace($Text, '(?m)^\s*ANTHROPIC_API_KEY\s*=', '# ANTHROPIC_API_KEY=')
$Utf8Bom = New-Object System.Text.UTF8Encoding($true)
[System.IO.File]::WriteAllText($EnvFile, $Text, $Utf8Bom)
Write-Host "OpenAIの設定を.envへ追加しました。" -ForegroundColor Green
Write-Host "OPENAI_API_KEYを設定して保存し、production\install.ps1を実行してください。" -ForegroundColor Cyan
Start-Process notepad.exe -ArgumentList $EnvFile
