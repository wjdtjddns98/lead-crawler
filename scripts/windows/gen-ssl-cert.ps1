# Generate a self-signed TLS cert (PEM) for serving the webapp over HTTPS
# on an internal network. SANs include localhost, the machine hostname and
# all local IPv4 addresses (plus any extras passed in), so browsers on other
# machines can reach it by IP or hostname.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\gen-ssl-cert.ps1
#   powershell ... -File scripts\windows\gen-ssl-cert.ps1 -DnsNames crawler.local -IpAddresses 10.0.0.5
#
# Output: certs\cert.pem + certs\key.pem  ->  leadcrawler web --host 0.0.0.0 `
#           --ssl-certfile certs\cert.pem --ssl-keyfile certs\key.pem
#
# NOTE (ASCII only): Windows PowerShell 5.1 misreads BOM-less UTF-8 .ps1 as cp949.

param(
    [string]$OutDir = "certs",
    [int]$Days = 825,
    [string[]]$DnsNames = @(),
    [string[]]$IpAddresses = @()
)

$ErrorActionPreference = "Stop"

# Locate openssl: PATH first, then the copy bundled with Git for Windows.
$openssl = $null
$cmd = Get-Command openssl -ErrorAction SilentlyContinue
if ($cmd) { $openssl = $cmd.Source }
if (-not $openssl) {
    foreach ($c in @("$env:ProgramFiles\Git\usr\bin\openssl.exe",
                     "$env:ProgramFiles\Git\mingw64\bin\openssl.exe")) {
        if (Test-Path $c) { $openssl = $c; break }
    }
}
if (-not $openssl) {
    Write-Error "openssl not found (install Git for Windows or add openssl to PATH)"
}

# SANs: localhost + hostname + every local IPv4 (skip loopback/APIPA) + user extras.
$dns = @("localhost", $env:COMPUTERNAME.ToLower()) + $DnsNames | Select-Object -Unique
$ips = @("127.0.0.1")
try {
    $ips += Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
        ForEach-Object { $_.IPAddress }
} catch {}
$ips = @($ips + $IpAddresses | Select-Object -Unique)
$san = (($dns | ForEach-Object { "DNS:$_" }) + ($ips | ForEach-Object { "IP:$_" })) -join ","

New-Item -ItemType Directory -Force $OutDir | Out-Null
$certPath = Join-Path $OutDir "cert.pem"
$keyPath = Join-Path $OutDir "key.pem"

& $openssl req -x509 -newkey rsa:2048 -nodes -sha256 `
    -keyout $keyPath -out $certPath -days $Days `
    -subj "/CN=leadcrawler" -addext "subjectAltName=$san"
if ($LASTEXITCODE -ne 0) { Write-Error "openssl failed (exit $LASTEXITCODE)" }

Write-Host "cert : $certPath"
Write-Host "key  : $keyPath"
Write-Host "SAN  : $san"
Write-Host ""
Write-Host "serve: leadcrawler web --host 0.0.0.0 --ssl-certfile $certPath --ssl-keyfile $keyPath"
