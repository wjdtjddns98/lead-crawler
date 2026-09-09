@echo off
rem KR 금융 신규발견 러너 (2026-08-20) - promote-segments 백필 완료를 기다렸다가
rem 연기금/증권/자산운용(KR, 17개 시/도 팬아웃) 신규 추출을 시작한다.
rem naver_local 키워드 = 연기금 / 증권 / 자산운용 ('증권·자산운용' 라벨 분해), NPS = 증권·자산운용 매핑분.
title leadcrawler-kr-finance-discovery
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist "logs" mkdir "logs"
set "LOG=logs\kr-finance-discovery.log"

echo [%date% %time%] watcher: promote-segments 완료 대기 >> %LOG%
:wait
findstr /c:"all segments completed" logs\promote-segments.log >nul 2>&1
if %errorlevel%==0 goto run
ping -n 121 127.0.0.1 >nul
goto wait

:run
echo [%date% %time%] discovery: KR 연기금/증권/자산운용 신규 추출 시작 >> %LOG%
.venv\Scripts\python.exe -m leadcrawler.cli run-global --industries "연기금,증권·자산운용" --countries KR --regions all --persist --out exports\kr-finance-new-20260820.xlsx >> %LOG% 2>&1
echo [%date% %time%] discovery: exited %errorlevel% >> %LOG%
pause
