@echo off
REM leadcrawler DB backup - pg_dump(-Fc) inside docker(leadcrawler-db) -> backups\, keep 14 days.
REM register (daily 03:00):
REM   schtasks /Create /TN leadcrawler-db-backup /SC DAILY /ST 03:00 /TR "%~f0"
setlocal
set "ROOT=%~dp0..\.."
set "OUT=%ROOT%\backups"
if not exist "%OUT%" mkdir "%OUT%"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%i"

docker exec leadcrawler-db pg_dump -U leadcrawler -d leadcrawler -Fc -f /tmp/lc_%TS%.dump
if errorlevel 1 (echo [backup-db] pg_dump FAILED & exit /b 1)
docker cp leadcrawler-db:/tmp/lc_%TS%.dump "%OUT%\leadcrawler_%TS%.dump"
if errorlevel 1 (echo [backup-db] docker cp FAILED & exit /b 1)
docker exec leadcrawler-db rm -f /tmp/lc_%TS%.dump

REM ponytail: local-only retention, add offsite copy when needed.
forfiles /p "%OUT%" /m leadcrawler_*.dump /d -14 /c "cmd /c del @path" 2>nul
echo [backup-db] done: %OUT%\leadcrawler_%TS%.dump
exit /b 0
