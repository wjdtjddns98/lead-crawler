# 키워드 크롤 재시도 (2026-08-21) - round2 step2 가 Playwright 행으로 중단된 뒤 재개.
# 공제회는 재검색돼도 dedup 으로 무해. 행 재발 시 외부에서 kill 후 재실행(멱등).
$Host.UI.RawUI.WindowTitle = 'leadcrawler-kr-keywords-retry'
Set-Location 'C:\Users\WSCOPY\Desktop\lead-crawler'
$env:PYTHONUTF8 = '1'
$log = 'logs\kr-keywords-retry.log'
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] retry: keyword crawl start"
& .venv\Scripts\python.exe -m leadcrawler.cli run-global --industries '공제회,자산운용사,증권사,투자자문' --countries KR --regions all --persist --out exports\kr-finance-keywords-20260821.xlsx *>> $log
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] retry: exited $LASTEXITCODE"
Read-Host 'done - press Enter to close'
