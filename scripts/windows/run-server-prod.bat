@echo off
title leadcrawler-server
cd /d "%~dp0..\.."
set PYTHONUTF8=1
.venv\Scripts\python.exe -m leadcrawler.cli web --host 0.0.0.0 --port 8000 --ssl-certfile certs\cert.pem --ssl-keyfile certs\key.pem
pause
