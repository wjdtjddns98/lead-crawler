# gBizINFO JP 도메인 백필 전량 (2026-08-27)
$Host.UI.RawUI.WindowTitle = 'leadcrawler-gbiz-backfill'
Set-Location 'C:\Users\WSCOPY\Desktop\lead-crawler'
$env:PYTHONUTF8 = '1'
# 토큰은 .env 의 LEADCRAWLER_GBIZINFO_API_TOKEN 을 Settings 가 읽는다(커밋본에서 평문 제거, 2026-09-09)
$log = 'logs\gbiz-backfill-20260827.log'
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] gbiz: start"
& .venv\Scripts\python.exe scripts\backfill_jp_domains_gbizinfo.py *>> $log
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] gbiz: exited $LASTEXITCODE"
Read-Host 'done'
