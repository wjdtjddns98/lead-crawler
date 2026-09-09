# KR 금융 2차 수확 (2026-08-21 PO 지시: nps-map + 키워드 확장 둘 다)
# ① NPS 업종코드 매핑 보충(미등재만 LLM) ② 변형 키워드 신규발견 ③ 매핑 반영 재크롤
$Host.UI.RawUI.WindowTitle = 'leadcrawler-kr-finance-round2'
Set-Location 'C:\Users\WSCOPY\Desktop\lead-crawler'
$env:PYTHONUTF8 = '1'
$log = 'logs\kr-finance-round2.log'

Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] round2: step1 nps-map-industries start"
& .venv\Scripts\python.exe -m leadcrawler.cli nps-map-industries *>> $log
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] round2: step1 exited $LASTEXITCODE"

Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] round2: step2 keyword crawl start"
& .venv\Scripts\python.exe -m leadcrawler.cli run-global --industries '공제회,자산운용사,증권사,투자자문' --countries KR --regions all --persist --out exports\kr-finance-keywords-20260821.xlsx *>> $log
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] round2: step2 exited $LASTEXITCODE"

Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] round2: step3 post-npsmap crawl start"
& .venv\Scripts\python.exe -m leadcrawler.cli run-global --industries '연기금,증권·자산운용' --countries KR --regions all --persist --out exports\kr-finance-post-npsmap-20260821.xlsx *>> $log
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] round2: step3 exited $LASTEXITCODE"

Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] round2: all done"
Read-Host 'round2 done - press Enter to close'
