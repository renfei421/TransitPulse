param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectDirectory
try {
    & docker compose -f compose.replay.yaml up -d --build
    if ($LASTEXITCODE -ne 0) { throw 'Local container startup failed' }
    Write-Host "Local replay: http://127.0.0.1:$Port/"
    Write-Host 'Keep this terminal open. This command makes no model API calls.'
    Write-Host 'On a fresh machine, restore the private archive first; see docs/ARCHIVE_AND_REPLAY.zh-CN.md.'
    & uv run --no-sync python -m scripts.serve_event_dashboard --local-api http://127.0.0.1:9092 --port $Port
} finally {
    Pop-Location
}
