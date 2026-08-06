@echo off
rem 백필 루프 러너 — %1 = A(fill-emails) 또는 C(backfill-resolve-domains).
rem --max-batches 도달 시 프로세스가 정상종료하고 여기서 재기동한다 = 완전한 메모리 리셋.
rem (2026-07-31 OOM: --loop 장기구동 중 Chromium/드라이버 누적으로 1.5h 만에 5.5GB — 백필은
rem  멱등이라 재기동해도 이어받는다. B(promote)는 인메모리 커서 때문에 이 러너 대상이 아님.)
rem 사용:  scripts\windows\run-backfill-loop.bat A
rem        scripts\windows\run-backfill-loop.bat C
rem 2번째 인자부터는 CLI 에 그대로 전달된다(예: ... A --country KR).
setlocal
cd /d "%~dp0..\.."
set PYTHONUTF8=1
rem 리다이렉트 대상 폴더가 없으면 cmd 가 python 실행 자체를 건너뛴다(조용한 무한 no-op).
if not exist "logs" mkdir "logs"

if /i "%1"=="A" (
  set "ARGS=fill-emails --loop --max-batches 20 --workers 2 %2 %3 %4 %5"
  set "LOG=logs\backfill-A-fill-emails.log"
) else if /i "%1"=="C" (
  set "ARGS=backfill-resolve-domains --loop --max-batches 20 --workers 2 %2 %3 %4 %5"
  set "LOG=logs\backfill-C-resolve-domains.log"
) else (
  echo 사용법: %~nx0 A^|C
  exit /b 1
)

:loop
echo [%date% %time%] runner: (re)start %1 >> %LOG%
.venv\Scripts\python.exe -m leadcrawler.cli %ARGS% >> %LOG% 2>&1
echo [%date% %time%] runner: exited %errorlevel% — 60s 후 재기동 >> %LOG%
rem timeout /t 는 stdin 리다이렉트(Start-Process·스케줄러)면 즉시 반환(errorlevel 125 실측)
rem → 백오프가 증발해 타이트 루프가 된다. ping 은 stdin 무관하게 60초를 보장한다.
ping -n 61 127.0.0.1 >nul
goto loop
