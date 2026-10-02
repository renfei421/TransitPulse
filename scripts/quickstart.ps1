# Run from PowerShell: .\scripts\quickstart.ps1
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
uv sync --frozen --extra notebook
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
docker compose --profile demo up -d --build --wait
if ($LASTEXITCODE -ne 0) { throw 'Local services failed to start; inspect docker compose logs' }
& '.\.venv\Scripts\python.exe' -m scripts.execute_notebook
if ($LASTEXITCODE -ne 0) { throw 'Notebook execution failed' }
Write-Host 'API: http://127.0.0.1:9090/api/v1'
Write-Host "Analysis: $projectRoot\artifacts\analysis.html"
