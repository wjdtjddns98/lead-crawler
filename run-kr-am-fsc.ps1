# KR 자산운용사 전량 발견+추출 (2026-08-25 PO 지시: '증권·자산운용' FSC 검색 모드 전량)
# 실행 트리 = dev 워크트리(FSC 검색 모드 머지분). 라이브 서버 트리(prod)와 분리.
$Host.UI.RawUI.WindowTitle = 'leadcrawler-kr-am-fsc'
Set-Location 'C:\claude\C--Users-WSCOPY-Desktop-lead-crawler\3a68e483-c179-4c86-8474-20275dbe30e5\scratchpad\wt-fsckey'
$env:PYTHONUTF8 = '1'
$env:LEADCRAWLER_DISCOVERY_MAX_PER_SOURCE = '3000'
$log = 'C:\Users\WSCOPY\Desktop\lead-crawler\logs\kr-am-fsc-20260825.log'
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] kr-am-fsc: start"
& C:\Users\WSCOPY\Desktop\lead-crawler\.venv\Scripts\python.exe -m leadcrawler.cli run-global --industries '증권·자산운용' --countries KR --persist --out C:\Users\WSCOPY\Desktop\lead-crawler\exports\kr-am-fsc-20260825.xlsx *>> $log
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] kr-am-fsc: exited $LASTEXITCODE"
Read-Host 'kr-am-fsc done - press Enter to close'
