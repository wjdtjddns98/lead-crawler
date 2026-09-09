# JP 승격 v2 (2026-08-26) - pipeline.promote 1회성 스크립트(prod 트리, v1.15 코드), rc!=0 재기동
$Host.UI.RawUI.WindowTitle = 'leadcrawler-promote-jp'
Set-Location 'C:\Users\WSCOPY\Desktop\lead-crawler'
$env:PYTHONUTF8 = '1'
$log = 'logs\promote-jp-20260826.log'
while ($true) {
  Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] promote-jp2: (re)start"
  & .venv\Scripts\python.exe scripts\promote_jp_oneoff.py logs\promote-cursor-jp.txt 50 2 *>> $log
  $rc = $LASTEXITCODE
  Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] promote-jp2: exited $rc"
  if ($rc -eq 0 -or $rc -eq 3) { break }
  Start-Sleep -Seconds 60
}
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] promote-jp2: ALL DONE"
Read-Host 'done'
