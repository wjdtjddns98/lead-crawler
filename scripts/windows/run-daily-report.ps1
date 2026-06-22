<#
.SYNOPSIS
    lead-crawler daily auto-reporting runner (one-shot, for Windows Task Scheduler).

.DESCRIPTION
    Runs once per day: one crawl pass + Notion auto-reporting (daily report, scrum,
    status board) via the config-driven `report-daily` command. Unlike the resident
    `leadcrawler serve` process, this exits immediately, so it survives reboots/crashes.

    Industry / country / milestone are read from `.env` (report_* settings), NOT passed
    as arguments -- this avoids the Windows PowerShell 5.1 UTF-8 (no-BOM) literal pitfall
    where Korean text in a .ps1 is misread as cp949.

    Changes directory to the project root first, so `.env` (relative load) applies. Real
    Notion writes require LEADCRAWLER_DRY_RUN=false + LEADCRAWLER_NOTION_TOKEN in `.env`;
    otherwise it runs in dry_run (payloads only, no network).

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\run-daily-report.ps1
#>

# Project root = two levels up from this script (scripts\windows\). Needed for .env load.
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root

# Force UTF-8 end to end so Korean log output is preserved (default codepage is cp949).
# PYTHONUTF8 -> python emits UTF-8; Console.OutputEncoding -> PowerShell decodes the
# child's stdout as UTF-8 (under Task Scheduler there is no console, so it must be set).
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Prefer venv python, fall back to PATH python.
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

# Ensure log directory.
$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir "daily-report.log"

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $log -Value "[$ts] daily report START" -Encoding UTF8

# Config-driven one-shot. Output (stdout+stderr) appended to the log with indentation.
& $py -m leadcrawler.cli report-daily 2>&1 |
    ForEach-Object { Add-Content -Path $log -Value "    $_" -Encoding UTF8 }
$code = $LASTEXITCODE

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $log -Value "[$ts] daily report END (exit $code)" -Encoding UTF8
exit $code
