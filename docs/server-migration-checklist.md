# 서버 이전 체크리스트 (구서버 → 신서버)

> 작성: 2026-07-21. 내부망 prod 서버(git pull 반영 + `run-server-prod.bat` 기동) 기준.
> prod 현재 버전: v1.0.2 (9cfc639)

## 1. 구서버에서 챙길 것 (git에 없는 파일들)

| 항목 | 위치 | 비고 |
|---|---|---|
| **DB 최종 덤프** | `scripts\windows\backup-db.bat` 실행 → `backups\leadcrawler_*.dump` | **제일 중요.** 서버 내리기 직전에 한 번 더 |
| `.env` | 프로젝트 루트 | 모든 라이브 키(DART 3개·Serper·네이버 3앱·Notion·Anthropic·DB URL·워커 수·rate 등) |
| `certs\cert.pem`, `certs\key.pem` | `certs\` | 새 서버 IP가 바뀌면 `scripts\windows\gen-ssl-cert.ps1`로 재생성 권장 |
| ~~`run-server-prod.bat` 등 bat~~ | git에 커밋됨 | pull 로 넘어옴 — 복사 불필요 |
| (선택) `backups\` 과거 덤프, `logs\` | | 필요하면 |

서버 내리기 전: **진행 중 크롤 취소/완료 확인** 후 종료 (status=running 좀비 박제 전례).

## 2. 신서버에 설치할 프로그램 5개

1. **Git** — 저장소 clone/pull
2. **Docker Desktop** — PostgreSQL 16 컨테이너 (`docker-compose.yml`)
3. **uv** — 파이썬 의존성 관리 (Python 3.10+도 uv가 알아서 설치)
4. **Node.js LTS** — 프론트 빌드 (`web/dist`는 git 미추적 → 서버에서 직접 빌드)
5. **Tesseract OCR** — OCR escalation용. UB Mannheim 빌드 + **kor 언어데이터**.
   OCR 안 되면 `TESSDATA_PREFIX` 환경변수 확인 (과거 함정)

## 3. 셋업 순서

```powershell
# ① 클론 + prod 체크아웃
git clone https://github.com/wjdtjddns98/lead-crawler.git
cd lead-crawler
git checkout prod

# ② 챙겨온 파일 복사: .env, certs\, run-server-prod.bat 등 (§1 표)

# ③ 파이썬 의존성 — extras 전부(playwright·OCR·psycopg·fastapi·anthropic 포함)
uv sync --all-extras
uv run playwright install chromium     # 헤드리스 브라우저 바이너리

# 설치 스모크(조용한 누락 방지 — §5 참고)
uv run python -c "import playwright, pytesseract, anthropic, psycopg, fastapi"

# ④ DB 기동 + 데이터 복원
docker compose up -d
docker cp backups\leadcrawler_최종.dump leadcrawler-db:/tmp/restore.dump
docker exec leadcrawler-db pg_restore -U leadcrawler -d leadcrawler --clean --if-exists /tmp/restore.dump
uv run leadcrawler db-upgrade          # 마이그레이션 head 일치 확인

# ⑤ 프론트 빌드
cd web
npm ci
npm run build
cd ..

# ⑥ 방화벽 8000 인바운드 허용 (관리자 PowerShell)
netsh advfirewall firewall add rule name="leadcrawler" dir=in action=allow protocol=TCP localport=8000

# ⑦ 기동 + 확인
scripts\windows\run-server-prod.bat
# 브라우저: https://<새IP>:8000/health → version 1.0.2 확인
```

## 4. 예약 작업 재등록 (구서버 것은 안 넘어옴)

```powershell
# DB 일일 백업 (03:00)
schtasks /Create /TN leadcrawler-db-backup /SC DAILY /ST 03:00 /TR "<프로젝트경로>\scripts\windows\backup-db.bat"
```

## 5. 함정 (과거 실사고 기준)

- **playwright·pytesseract·anthropic 누락 = 무증상 사고**: 에러 없이 정적 추출만 돌아
  이메일 수율 급락 / AI 소스 조용히 스킵. §3의 스모크 import 필수.
- **`.venv` 위치**: `run-server-prod.bat`이 `.venv\Scripts\python.exe`를 직접 부름 →
  `uv sync`가 만든 `.venv`가 프로젝트 루트에 있어야 함.
- **서버 재시작은 `taskkill /f /t`**: 자식 고아 프로세스가 포트 잡는 사고 전례.
  포트 리스너 PID로 잡을 것 (상대경로 실행 서버는 이름 필터 탈출).
- **prod는 -NoReload**: 리로드 켜면 git pull 시 크롤 끊김.
- PowerShell 5.1에서 https 로컬 확인은 `curl -k` (TLS/자체서명 함정).
- 재부팅 후 Docker Desktop 자동시작 확인 (재부팅으로 서버/Docker/PG 전멸 전례).
