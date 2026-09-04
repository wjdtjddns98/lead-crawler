@echo off
title dart-cache-fill
rem DART corp 캐시 잔여분(~44.6k) 적재 후 NPS relink 소급 교정. KST 자정 쿼터 리셋 후 실행.
cd /d "%~dp0..\.."
set PYTHONUTF8=1
.venv\Scripts\python.exe -m leadcrawler.cli dart-cache-fill
if errorlevel 1 (
  echo [중단] 캐시 적재 실패/예산 소진 — relink 는 건너뜀. 로그 확인 후 재실행.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m leadcrawler.cli nps-relink-dart
pause
