# KR 금융(연기금/증권/자산운용) 야간 반복 신규추출 (2026-08-20 PO 지시: 밤새 계속)
# cmd bat 러너가 성공 경로 goto에서 증발하는 미규명 이슈가 있어 PowerShell 루프로 구동.
$Host.UI.RawUI.WindowTitle = 'leadcrawler-kr-finance-overnight'
Set-Location 'C:\Users\WSCOPY\Desktop\lead-crawler'
$env:PYTHONUTF8 = '1'
$log = 'logs\kr-finance-overnight.log'
$pass = 0
while ($true) {
    $pass++
    Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] overnight: pass $pass start"
    & .venv\Scripts\python.exe -m leadcrawler.cli run-global --industries '연기금,증권·자산운용' --countries KR --regions all --persist --out exports\kr-finance-overnight.xlsx *>> $log
    Add-Content $log "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] overnight: pass $pass exited $LASTEXITCODE"
    Start-Sleep -Seconds 300
}
