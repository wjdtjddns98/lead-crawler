# KR 금융(자산운용·연기금·자문) 도메인 resolve 1회성 (2026-08-24) - 네이버 1차라 Serper 소진 무관
$Host.UI.RawUI.WindowTitle = 'leadcrawler-resolve-fin-kr'
Set-Location 'C:\Users\WSCOPY\Desktop\lead-crawler'
$env:PYTHONUTF8 = '1'
$log = 'logs\resolve-fin-kr.log'
while ($true) {
    Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runner: (re)start resolve-fin-kr"
    & .venv\Scripts\python.exe -m leadcrawler.cli backfill-resolve-domains --country KR --industry '연기금,증권·자산운용,투자자문' --batch 400 --workers 4 --stall-exit-secs 900 *>> $log
    $rc = $LASTEXITCODE
    Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runner: exited $rc"
    if ($rc -eq 0) { break }
    Start-Sleep -Seconds 60
}
Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] runner: resolve-fin-kr done"
Read-Host 'resolve-fin-kr done - press Enter to close'
