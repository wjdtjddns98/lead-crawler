# 국가 빈값 LLM 백필 러너 (2026-08-21 PO 지시) - 잔여 ~2,970행, 예산가드/캡 내.
$Host.UI.RawUI.WindowTitle = 'leadcrawler-country-llm-backfill'
Set-Location 'C:\Users\WSCOPY\Desktop\lead-crawler'
$env:PYTHONUTF8 = '1'
$log = 'logs\country-llm-backfill.log'
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] country-llm: start"
& .venv\Scripts\python.exe scripts\backfill_country_llm.py *>> $log
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] country-llm: exited $LASTEXITCODE"
Read-Host 'done - press Enter to close'
