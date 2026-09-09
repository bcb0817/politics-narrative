param(
    [Parameter(Mandatory=$true)][string]$Quarantine,
    [datetimeoffset]$Cutoff = '2026-09-08T00:00:00+09:00',
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$backupBase = Join-Path $root 'backups'
$destination = (Resolve-Path -LiteralPath $Quarantine).Path
if (-not $destination.StartsWith($backupBase + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Quarantine must be an existing repository backup child'
}
$retired = Join-Path $destination 'retired-runtime'

function Assert-ContainedFile([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Outside repository' }
    $node = Get-Item -LiteralPath $full -Force
    while ($node -and $node.FullName -ne $root) {
        if ($node.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse points are excluded' }
        $node = $node.Parent
        if (-not $node -and [IO.File]::Exists($full)) { $node = (Get-Item -LiteralPath $full).Directory; $full = $node.FullName }
    }
}

$tracked = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
& git -C $root ls-files | ForEach-Object { [void]$tracked.Add($_) }
if ($LASTEXITCODE -ne 0) { throw 'Cannot verify tracked files' }
$files = [Collections.Generic.List[object]]::new()
$folders = @('logs','outputs','reports','data/article_content_cache','data/political_sentiment_cache',
    'data/politics_analysis_cache','data/politics_candidate_cache','data/daily_reviews',
    'data/x_search_history','data/xai_search_history','knowledge/viral_patterns')
foreach ($folder in $folders) {
    $base = Join-Path $root $folder
    if (Test-Path -LiteralPath $base) {
        Get-ChildItem -LiteralPath $base -Recurse -File -Force | ForEach-Object { $files.Add($_) }
    }
}
foreach ($relative in @('bot.log','data/daily_review_latest.json','data/reach_report_latest.json',
    'data/x_search_latest.json','data/xai_search_latest.json','data/chatgpt_strategy_history.jsonl')) {
    $file = Join-Path $root $relative
    if (Test-Path -LiteralPath $file) { $files.Add((Get-Item -LiteralPath $file)) }
}
$actions = [Collections.Generic.List[object]]::new()
Assert-ContainedFile $destination
foreach ($file in $files) {
    Assert-ContainedFile $file.FullName
    $relative = $file.FullName.Substring($root.Length + 1).Replace('\','/')
    if ($tracked.Contains($relative) -or $relative -match '^outputs/topical-astra-') { continue }
    $isLog = $relative -match '^(logs/.*\.(log|jsonl)|bot\.log)$'
    $keep = $null
    $removedLines = 0
    if ($file.LastWriteTimeUtc -ge $Cutoff.UtcDateTime) {
        if (-not $isLog) { continue }
        $lines = [IO.File]::ReadAllLines($file.FullName)
        $keep = [Collections.Generic.List[string]]::new()
        $remove = $false  # Undated preambles are retained, not guessed old.
        foreach ($line in $lines) {
            $stamp = $null
            if ($line.TrimStart().StartsWith('{')) {
                try {
                    $record = $line | ConvertFrom-Json
                    foreach ($key in @('ts_jst','created_at','timestamp','time','at')) {
                        if ($record.$key) { $stamp = [string]$record.$key; break }
                    }
                } catch { }
            } elseif ($line -match '^\[?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)') {
                $stamp = $Matches[1]
            }
            if ($stamp) {
                try {
                    if ($stamp -notmatch '(Z|[+-]\d{2}:\d{2})$') { $stamp += '+09:00' }
                    $remove = ([datetimeoffset]::Parse($stamp) -lt $Cutoff)
                } catch { $remove = $false }
            }
            if ($remove) { $removedLines++ } else { $keep.Add($line) }
        }
        if ($removedLines -eq 0) { continue }
    }
    $target = [IO.Path]::GetFullPath((Join-Path $retired $relative))
    if (-not $target.StartsWith($retired + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe destination' }
    if (Test-Path -LiteralPath $target) { throw "Quarantine target already exists: $relative" }
    if ($Apply) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        if ($null -eq $keep -or $keep.Count -eq 0) {
            Move-Item -LiteralPath $file.FullName -Destination $target
        } else {
            Copy-Item -LiteralPath $file.FullName -Destination $target
            [IO.File]::WriteAllLines($file.FullName, $keep, [Text.UTF8Encoding]::new($false))
        }
    }
    $actions.Add([pscustomobject]@{file=$relative; bytes=$file.Length; removedLines=$removedLines;
        action=$(if ($null -eq $keep -or $keep.Count -eq 0) {'retire_file'} else {'trim_log'})})
}
$result = [pscustomobject]@{applied=[bool]$Apply; cutoff=$Cutoff.ToString('o'); files=$actions.Count;
    originalBytes=($actions | Measure-Object bytes -Sum).Sum; actions=$actions}
if ($Apply) {
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $destination 'cleanup-manifest.json') -Encoding utf8
}
$result | ConvertTo-Json -Depth 5
