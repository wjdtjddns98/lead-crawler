<#
.SYNOPSIS
    Register the lead-crawler daily reporting job with Windows Task Scheduler.

.DESCRIPTION
    Creates a scheduled task that runs run-daily-report.ps1 every day at a given time.
    OS-triggered instead of a resident project scheduler (`leadcrawler serve`), so it
    survives reboots and needs no long-running daemon.

    - Default runs "only when logged on" (no credentials needed). To run while logged
      off, pass -RunWhetherLoggedOn (requires admin PowerShell + S4U).
    - If the PC was off at the scheduled time, it runs at the next available time
      (StartWhenAvailable).

.PARAMETER Time
    Daily run time "HH:mm" (default "09:00", local time).

.PARAMETER TaskName
    Task name (default "LeadCrawlerDailyReport").

.PARAMETER RunWhetherLoggedOn
    Run while logged off (S4U). Requires admin PowerShell.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\register-daily-task.ps1

.EXAMPLE
    powershell -File scripts\windows\register-daily-task.ps1 -Time 08:30 -RunWhetherLoggedOn
#>
param(
    [string]$Time = "09:00",
    [string]$TaskName = "LeadCrawlerDailyReport",
    [switch]$RunWhetherLoggedOn
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$runner = Join-Path $PSScriptRoot "run-daily-report.ps1"
if (-not (Test-Path $runner)) { throw "Runner not found: $runner" }

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`"" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -Daily -At $Time

# Catch up missed runs + cap runaway runs at 2 hours; skip if an instance is still running.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

$desc = "lead-crawler daily crawl + Notion auto-reporting (daily/scrum/status)"

if ($RunWhetherLoggedOn) {
    # S4U: run without an interactive logon (needs admin). No password stored.
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Description $desc -Force | Out-Null
} else {
    # Default: current user, only when logged on (no credentials needed).
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description $desc -Force | Out-Null
}

Write-Host "Registered '$TaskName' to run daily at $Time."
Write-Host "Run once now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Check status:  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "Remove:        Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Write-Host "Log file:      $($root)\logs\daily-report.log"
