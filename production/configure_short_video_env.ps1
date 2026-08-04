param(
    [string]$EnvPath = "",
    [string]$PublicBaseUrl = "",
    [string]$FfmpegPath = "",
    [string]$FfprobePath = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $EnvPath) {
    $EnvPath = Join-Path $root ".env"
}
if (-not $FfmpegPath) {
    $FfmpegPath = Join-Path $env:LOCALAPPDATA (
        "PoliticsNarrativeTools\ffmpeg\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe")
}
if (-not $FfprobePath) {
    $FfprobePath = Join-Path $env:LOCALAPPDATA (
        "PoliticsNarrativeTools\ffmpeg\ffmpeg-master-latest-win64-gpl\bin\ffprobe.exe")
}
if (-not (Test-Path -LiteralPath $EnvPath)) {
    throw "Environment file was not found."
}
if (-not (Test-Path -LiteralPath $FfmpegPath)) {
    throw "FFmpeg executable was not found."
}
if (-not (Test-Path -LiteralPath $FfprobePath)) {
    throw "FFprobe executable was not found."
}

$updates = [ordered]@{
    "FFMPEG_PATH" = $FfmpegPath
    "FFPROBE_PATH" = $FfprobePath
    "SHORT_VIDEO_FACTORY_ENABLED" = "true"
    "SHORT_VIDEO_OPERATION_PHASE" = "A"
    "SHORT_VIDEO_AUTO_PUBLISH_ENABLED" = "false"
    "SHORT_VIDEO_X_AUTO_PUBLISH" = "false"
    "SHORT_VIDEO_THREADS_AUTO_PUBLISH" = "false"
    "SHORT_VIDEO_YOUTUBE_AUTO_PUBLISH" = "false"
    "SHORT_VIDEO_INSTAGRAM_AUTO_PUBLISH" = "false"
    "SHORT_VIDEO_TTS_PROVIDER" = "openai"
    "SHORT_VIDEO_TTS_MODEL" = "gpt-4o-mini-tts"
    "SHORT_VIDEO_TTS_VOICE" = "coral"
    "SHORT_VIDEO_TTS_SPEED" = "0.9"
    "SHORT_VIDEO_PUBLIC_MEDIA_TTL_MINUTES" = "180"
    "SHORT_VIDEO_MEDIA_SERVER_ENABLED" = "true"
    "SHORT_VIDEO_MEDIA_SERVER_HOST" = "127.0.0.1"
    "SHORT_VIDEO_MEDIA_SERVER_PORT" = "8766"
    "SHORT_VIDEO_DISCORD_ENABLED" = "true"
}
if ($PublicBaseUrl) {
    $updates["SHORT_VIDEO_PUBLIC_MEDIA_BASE_URL"] = $PublicBaseUrl.TrimEnd("/")
}

$lines = [System.Collections.Generic.List[string]]::new()
Get-Content -LiteralPath $EnvPath -Encoding UTF8 | ForEach-Object {
    $lines.Add($_)
}
foreach ($entry in $updates.GetEnumerator()) {
    $pattern = "^\s*" + [Regex]::Escape($entry.Key) + "\s*="
    $replaced = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match $pattern) {
            $lines[$index] = "$($entry.Key)=$($entry.Value)"
            $replaced = $true
        }
    }
    if (-not $replaced) {
        $lines.Add("$($entry.Key)=$($entry.Value)")
    }
}
[System.IO.File]::WriteAllLines(
    $EnvPath, $lines, [System.Text.UTF8Encoding]::new($false))

Write-Host "Short-video environment settings updated safely."
Write-Host "Auto publish remains disabled."
