# Windows 운영 스크립트

내부망 prod 서버(Windows)에서 쓰는 배치·PowerShell 러너 모음.

| 파일 | 역할 |
|---|---|
| `run-server-prod.bat` | prod 서버 기동(HTTPS) |
| `serve-https.ps1` · `gen-ssl-cert.ps1` | 자체서명 인증서 생성 + HTTPS 기동 |
| `backup-db.bat` | DB 백업 (예약 작업 `leadcrawler-db-backup`, 매일 03:00) |
| `dart-cache-fill-then-relink.bat` | DART 캐시 채움 → NPS 재연결 |
| `run-backfill-loop.bat` | 백필 루프 실행 |

## 일일 Notion 리포팅 (제거됨)

`register-daily-task.ps1` / `run-daily-report.ps1` 로 등록하던 **`LeadCrawlerDailyReport`
예약 작업은 폐지**됐다. 데일리 스크럼·리포트는 Nutti 허브 단위 단일 스케줄에서 작성한다.

이미 등록해 둔 서버가 있으면 아래로 해제한다:

```powershell
Unregister-ScheduledTask -TaskName LeadCrawlerDailyReport -Confirm:$false
```
