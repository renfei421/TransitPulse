param(
    [ValidateRange(1024,65535)][int]$Port = 8088,
    [string]$Kubeconfig
)
$ErrorActionPreference = 'Stop'
if (-not $Kubeconfig) {
    $cloudDirectory = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
    $Kubeconfig = Join-Path $cloudDirectory 'k8s-1-34-10-do-5-sgp1-1790830229700-kubeconfig.yaml'
}
$resolvedConfig = (Resolve-Path -LiteralPath $Kubeconfig).Path
$cloudContext = 'do-sgp1-k8s-1-34-10-do-5-sgp1-1790830229700'
Write-Host "Cloud API: http://127.0.0.1:$Port/api/v1/meta"
Write-Host 'Keep this terminal open. Ctrl+C closes the local tunnel; it does not stop cloud services.'
& kubectl --kubeconfig $resolvedConfig --context $cloudContext -n fission port-forward service/router "${Port}:80" --address=127.0.0.1
if ($LASTEXITCODE -ne 0) { throw "kubectl port-forward failed (exit $LASTEXITCODE)" }
