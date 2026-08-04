param(
    [Parameter(Mandatory = $true)][string]$TextPath,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [string]$Voice = "Microsoft Haruka Desktop",
    [int]$Rate = -1,
    [int]$Volume = 100
)

$ErrorActionPreference = "Stop"
$resolvedText = (Resolve-Path -LiteralPath $TextPath).Path
$outputDirectory = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
if (Test-Path -LiteralPath $OutputPath) {
    Remove-Item -LiteralPath $OutputPath -Force
}
$text = Get-Content -LiteralPath $resolvedText -Raw -Encoding UTF8
# SAPI 5 desktop voices may fail the entire utterance on emoji, private-use
# glyphs, or malformed symbols. Keep natural-language letters, numbers,
# punctuation and whitespace; the on-screen version retains the original.
$text = [regex]::Replace(
    $text,
    '[^\p{L}\p{M}\p{N}\p{P}\p{Z}\r\n]',
    ''
)

$speaker = New-Object -ComObject SAPI.SpVoice
$stream = New-Object -ComObject SAPI.SpFileStream
try {
    if ($Voice) {
        $selected = @($speaker.GetVoices() | Where-Object {
            $_.GetDescription() -like "$Voice*"
        } | Select-Object -First 1)
        if ($selected.Count -gt 0) {
            $speaker.Voice = $selected[0]
        }
    }
    $speaker.Rate = [Math]::Max(-10, [Math]::Min(10, $Rate))
    $speaker.Volume = [Math]::Max(0, [Math]::Min(100, $Volume))
    # SSFMCreateForWrite = 3. SAPI writes a standard PCM WAV container.
    $stream.Open($OutputPath, 3, $false)
    $speaker.AudioOutputStream = $stream
    [void]$speaker.Speak($text)
}
finally {
    try { $stream.Close() } catch {}
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($stream)
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($speaker)
}

$outputFile = Get-Item -LiteralPath $OutputPath
if ($outputFile.Length -le 44) {
    throw "SAPI produced an empty or invalid WAV file: $OutputPath"
}
$outputFile | Select-Object FullName, Length, LastWriteTime
