# JP 상장사 발견 런 (2026-08-26) - EDINET 3,817사 원장 적재(발견+persist)
$Host.UI.RawUI.WindowTitle = 'leadcrawler-jp-discovery'
Set-Location 'C:\claude\C--Users-WSCOPY-Desktop-lead-crawler\3a68e483-c179-4c86-8474-20275dbe30e5\scratchpad\wt-fsckey'
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = 'C:\claude\C--Users-WSCOPY-Desktop-lead-crawler\3a68e483-c179-4c86-8474-20275dbe30e5\scratchpad\wt-fsckey'
$env:LEADCRAWLER_DISCOVERY_MAX_PER_SOURCE = '5000'
$log = 'C:\Users\WSCOPY\Desktop\lead-crawler\logs\jp-discovery-20260826.log'
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] jp-discovery: start"
& C:\Users\WSCOPY\Desktop\lead-crawler\.venv\Scripts\python.exe -m leadcrawler.cli run-global --industries '전체' --countries JP --persist --out C:\Users\WSCOPY\Desktop\lead-crawler\exports\jp-listed-20260826.xlsx *>> $log
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] jp-discovery: exited $LASTEXITCODE"
Read-Host 'done'
