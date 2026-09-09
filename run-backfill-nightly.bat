@echo off
rem 야간 도메인 백필 — 크롤과 분리(#323, 2026-07-27). 스케줄드 태스크
rem "LeadCrawler-NightlyBackfill"(매일 03:00)이 실행. 로그 = backfill-nightly.log (append).
rem 배치 5000 = 네이버 무료쿼터(25k/일) 내에서 크롤 inline 해석 몫을 남긴 보수치.
cd /d C:\Users\WSCOPY\Desktop\lead-crawler
set PYTHONUTF8=1
echo [%date% %time%] nightly backfill start >> backfill-nightly.log
.venv\Scripts\python.exe -m leadcrawler.cli backfill-resolve-domains --batch 5000 --workers 4 >> backfill-nightly.log 2>&1
echo [%date% %time%] nightly backfill end (exit %errorlevel%) >> backfill-nightly.log
