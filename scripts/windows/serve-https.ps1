# One-shot HTTPS launcher for internal-network deployment:
# generates a self-signed cert on first run (certs\), then starts the webapp
# on 0.0.0.0 with TLS. Re-runs reuse the existing cert.
#
# Usage:  serve-https.bat            (repo root, double-clickable)
#    or:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\serve-https.ps1 [-Port 8000]
#
# Auto-reload (default ON, opt out with -NoReload):
#   - backend: uvicorn --reload restarts on *.py changes (in-flight crawls die on restart)
#   - frontend: "vite build --watch" in a second window rebuilds web\dist on change
#     (browser refresh picks it up; needs web\node_modules)
#
# NOTE (ASCII only): Windows PowerShell 5.1 misreads BOM-less UTF-8 .ps1 as cp949.

param(
    [int]$Port = 8000,
    [string]$BindHost = "0.0.0.0",
    [switch]$NoReload
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
if (-not $python) { Write-Error "venv not found ($root\.venv) - run: uv sync --extra api --extra db" }

if (-not (Test-Path (Join-Path $root "web\dist\index.html"))) {
    Write-Host "note: web\dist not found - UI will 404 at / (build once: cd web; npm install; npm run build)"
}

# frontend watch build - keeps web\dist fresh; served per-request, so a browser
# refresh is enough (no server restart). Separate visible window by design.
$viteProc = $null
$extraArgs = @()
if (-not $NoReload) {
    $extraArgs += "--reload"
    if (Test-Path (Join-Path $root "web\node_modules")) {
        Write-Host "starting frontend watch build (web\dist auto-rebuild)..."
        $viteProc = Start-Process -PassThru -FilePath "cmd.exe" `
            -WorkingDirectory (Join-Path $root "web") `
            -ArgumentList "/c", "title vite-watch & npx vite build --watch"
    } else {
        Write-Host "note: web\node_modules not found - frontend watch skipped (cd web; npm install)"
    }
}

Write-Host "serving https://$($env:COMPUTERNAME.ToLower()):$Port (Ctrl+C to stop)"
try {
    & $python -m leadcrawler.cli web --host $BindHost --port $Port `
        --ssl-certfile $cert --ssl-keyfile $key @extraArgs
} finally {
    # kill the whole vite window tree (cmd -> node) or it outlives Ctrl+C
    if ($viteProc -and -not $viteProc.HasExited) {
        & taskkill /pid $viteProc.Id /t /f | Out-Null
    }
}
