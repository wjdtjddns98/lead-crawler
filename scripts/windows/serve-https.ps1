# One-shot HTTPS launcher for internal-network deployment:
# generates a self-signed cert on first run (certs\), then starts the webapp
# on 0.0.0.0 with TLS. Re-runs reuse the existing cert.
#
# Usage:  serve-https.bat            (repo root, double-clickable)
#    or:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\serve-https.ps1 [-Port 8000]
#
# NOTE (ASCII only): Windows PowerShell 5.1 misreads BOM-less UTF-8 .ps1 as cp949.

param(
    [int]$Port = 8000,
    [string]$BindHost = "0.0.0.0"
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

$certDir = Join-Path $root "certs"
$cert = Join-Path $certDir "cert.pem"
$key = Join-Path $certDir "key.pem"
if (-not ((Test-Path $cert) -and (Test-Path $key))) {
    Write-Host "no cert found - generating self-signed cert..."
    & powershell -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $PSScriptRoot "gen-ssl-cert.ps1") -OutDir $certDir
    if ($LASTEXITCODE -ne 0) { Write-Error "cert generation failed" }
}

# venv python (console scripts may be stale - module invocation is reliable)
$python = $null
foreach ($v in @(".venv", "venv")) {
    $p = Join-Path $root "$v\Scripts\python.exe"
    if (Test-Path $p) { $python = $p; break }
}
if (-not $python) { Write-Error "venv not found ($root\.venv) - run: python -m venv .venv; pip install -e .[api,db]" }

Write-Host "serving https://$($env:COMPUTERNAME.ToLower()):$Port (Ctrl+C to stop)"
& $python -m leadcrawler.cli web --host $BindHost --port $Port `
    --ssl-certfile $cert --ssl-keyfile $key
