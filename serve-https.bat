@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\serve-https.ps1" %*
pause
