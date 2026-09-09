# FINE asset-manager one-off: promote-only (KR / securities-AM scope). 2026-09-01.
# ASCII only on purpose (PS 5.1 reads BOM-less UTF-8 as ANSI).
# Stall watchdog exits with rc 86 (one hung site); cursor file persists, so just retry.
$Host.UI.RawUI.WindowTitle = 'leadcrawler-fine-am-oneoff'
Set-Location 'C:\Users\WSCOPY\Desktop\lead-crawler'
$env:PYTHONUTF8 = '1'
$log = 'C:\Users\WSCOPY\Desktop\lead-crawler\logs\fine-am-oneoff-20260901-run3.log'
$attempt = 0
do {
  $attempt++
  Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] fine-am: start attempt $attempt"
  & C:\Users\WSCOPY\Desktop\lead-crawler\.venv\Scripts\python.exe scripts\fine_am_oneoff_run.py 10 3 --promote-only *>> $log
  $rc = $LASTEXITCODE
  Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] fine-am: attempt $attempt exited $rc"
} while ($rc -eq 86 -and $attempt -lt 12)
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] fine-am: exited $rc (attempts $attempt)"
Read-Host 'fine-am done - press Enter to close'
