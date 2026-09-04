# Windows 작업 스케줄러 — 일일 자동 리포팅

프로젝트 단위 상주 스케줄러(`leadcrawler serve`) 대신, **Windows OS 가 매일 한 번**
`report-auto`(크롤 1회전 + Notion 일일보고·스크럼·현황 자동 기입)를 실행하게 한다.
상주 프로세스가 없어 재부팅·크래시에 강하다.

## 구성

| 파일 | 역할 |
|---|---|
| `run-daily-report.ps1` | 작업이 매일 호출하는 one-shot 러너(프로젝트 루트 이동 → venv python → `report-daily` → `logs/daily-report.log` 적재) |
| `register-daily-task.ps1` | 위 러너를 매일 지정 시각에 실행하는 예약 작업 등록 |

업종·국가·마일스톤은 `.env`(`LEADCRAWLER_REPORT_INDUSTRIES` / `_COUNTRIES` / `_MILESTONE`)에서
읽는다. 러너(.ps1)에는 한글 인자가 없다 — Windows PowerShell 5.1 이 BOM 없는 UTF-8 .ps1 의
한글을 cp949 로 오독하는 문제를 피하기 위함(설정은 파이썬이 UTF-8 로 안전하게 읽음).

## 사용

```powershell
# 매일 09:00 등록(로그인 상태에서만 실행 — 자격증명 불필요)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\register-daily-task.ps1 -Time 09:00

# 즉시 1회 테스트
Start-ScheduledTask -TaskName LeadCrawlerDailyReport

# 마지막 실행 결과 확인
Get-ScheduledTask -TaskName LeadCrawlerDailyReport | Get-ScheduledTaskInfo

# 삭제
Unregister-ScheduledTask -TaskName LeadCrawlerDailyReport -Confirm:$false
```

로그아웃/잠금 상태에서도 돌리려면(관리자 PowerShell): `-RunWhetherLoggedOn`.

## 실제 Notion 기입 전제 (중요)

러너는 프로젝트 루트의 `.env` 를 읽는다. **기본은 dry_run** 이라 payload 만 만들고
네트워크 호출을 하지 않는다. 실제로 Notion 에 쓰려면 `.env` 에 최소:

```dotenv
LEADCRAWLER_DRY_RUN=false
LEADCRAWLER_NOTION_TOKEN=<Notion 통합 시크릿>   # 일일/스크럼/현황 3개 DB에 공유돼 있어야 함
# (KR 라이브 발견까지 하려면) LEADCRAWLER_DART_API_KEY=... , LEADCRAWLER_DATABASE_URL=...
```

`.env` 가 없으면 작업은 정상 실행되지만 dry_run 으로만 동작한다(로그로 확인 가능).
