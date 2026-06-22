# 검증 워크벤치 프론트엔드 (Vite + React + TS)

직원이 검증 큐를 검토 → 후보 확정/거부 → 확정분 엑셀을 내려받는 내부 UI.
백엔드(FastAPI, `leadcrawler web`)의 `/queue`·`/export` 를 호출한다.

## 개발

```bash
# 1) 백엔드 기동(프로젝트 루트에서)
leadcrawler web            # http://127.0.0.1:8000

# 2) 프론트 dev 서버(web/ 에서)
npm install
npm run dev                # http://localhost:5173 (API 는 8000 으로 프록시)
```

dev 서버는 `/queue`·`/export`·`/health` 를 `VITE_API_TARGET`(기본 `http://127.0.0.1:8000`)
으로 프록시하므로 CORS 설정이 필요 없다.

## 빌드

```bash
npm run build              # 타입체크 + dist/ 정적 산출
npm run preview            # 빌드 결과 미리보기
```

운영 배포 시 백엔드가 다른 출처면 `VITE_API_BASE=https://api.example.com` 로 빌드한다.

## 구조

- `src/api.ts` — 타입 안전 API 클라이언트
- `src/types.ts` — 백엔드 DTO 대응 타입
- `src/App.tsx` — 큐 목록·필터·확정/거부·export·페이지네이션
- `src/components/` — `QueueTable`, `StatusBadge`
