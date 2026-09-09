# KR 연기금 키워드 크롤 (2026-08-25 PO 지시: 연기금·공제회·공제조합 — FSC 미커버 보충)
# 8/21 공제회 키워드 크롤과 동일 경로(검색 기반). dedup 으로 기수확분 무해.
$Host.UI.RawUI.WindowTitle = 'leadcrawler-kr-pension'
Set-Location 'C:\claude\C--Users-WSCOPY-Desktop-lead-crawler\3a68e483-c179-4c86-8474-20275dbe30e5\scratchpad\wt-fsckey'
$env:PYTHONUTF8 = '1'
$log = 'C:\Users\WSCOPY\Desktop\lead-crawler\logs\kr-pension-20260825.log'
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] kr-pension: start"
& C:\Users\WSCOPY\Desktop\lead-crawler\.venv\Scripts\python.exe -m leadcrawler.cli run-global --industries '연기금,공제회,공제조합' --countries KR --regions all --persist --out C:\Users\WSCOPY\Desktop\lead-crawler\exports\kr-pension-20260825.xlsx *>> $log
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] kr-pension: exited $LASTEXITCODE"
Read-Host 'kr-pension done - press Enter to close'
