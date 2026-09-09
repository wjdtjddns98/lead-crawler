# 업종 LLM 백필 러너 (2026-08-21 PO 지시: 업종은 백필로) - company 미분류 15.7k 소급 분류.
# 홈페이지 본문 있는 행만 분류(블라인드 금지·멱등), 월예산 가드는 ledger 가 집행.
$Host.UI.RawUI.WindowTitle = 'leadcrawler-industry-backfill'
Set-Location 'C:\Users\WSCOPY\Desktop\lead-crawler'
$env:PYTHONUTF8 = '1'
$env:LEADCRAWLER_INDUSTRY_LLM_MAX_CALLS = '50000'
$log = 'logs\industry-backfill.log'
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] industry-backfill: start"
& .venv\Scripts\python.exe -m leadcrawler.cli backfill-industry *>> $log
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] industry-backfill: exited $LASTEXITCODE"
Read-Host 'done - press Enter to close'
