# 금융위원회 금융회사기본정보 발견 소스 설계

상태: 구현 전 설계안. 이 문서는 코드 변경 범위와 검증 기준만 정의한다.

## 1. 목표와 결정 요약

공공데이터포털 “금융위원회_금융회사기본정보”의 최신 스냅샷을 순차 페이징해 한국
금융회사를 발견한다.

- 엔드포인트: GET
  https://apis.data.go.kr/1160100/service/GetFnCoBasiInfoService/getFnCoOutl
- 인증: 기존 Settings.data_go_kr_service_key 한 개를 serviceKey로 전달한다
  (leadcrawler/config.py:68-70). 새 설정이나 새 의존성은 추가하지 않는다.
- 산출: 회사명, 법인등록번호, 사업자번호, 주소, 전화, 홈페이지, 표준산업분류, 상장일만
  DiscoveredCompany의 기존 필드에 옮긴다. 대표자·설립일 등 현재 산출 모델에 자리가 없는
  필드는 버리고 모델을 확장하지 않는다 (leadcrawler/sources/base.py:42-74).
- 식별: registry=fsc, registry_id=crno를 우선 사용한다. crno가 없으면 기존
  build_company()가 도메인, 회사명+국가 순서로 폴백한다
  (leadcrawler/dedup.py:118-135, leadcrawler/sources/base.py:180-236).
- 실행: build_sources()에서 DART 바로 뒤, NPS보다 앞에 둔다. 기본 KR 화이트리스트에도
  반드시 추가한다 (leadcrawler/sources/registry.py:48-106, 175-186). KR에서 실효
  우선순위는 FSC → NPS → Naver Local이 된다.
- 방식: 목록 API 한 종류를 순차 호출한다. DART의 2패스, 키 로테이션, 법인 상세 캐시,
  최근공시 우선 레인, 소스 내부 병렬 청킹은 구현하지 않는다.

완료 기준:

1. 기본 kr_discovery_nps_only=True에서도 대상 KR 금융 세그먼트가 FSC를 실행한다.
2. 실행 간 커서로 원시 행을 이어 읽고 모집단 끝에서는 커서가 0으로 돌아간다.
3. dry_run=True에서는 Fetcher와 커서 저장소를 호출하지 않고 항상 같은 회사를 반환한다.
4. 홈페이지·주소·전화·사업자번호·상장 정보가 추가 호출 없이 기존 산출 필드에 보존된다.
5. 실패 시 마지막으로 처리 완료한 원시 행까지만 커서를 전진한다.

## 2. 파일과 클래스 구조

### 신규 모듈

leadcrawler/sources/fsc_financial_company.py

- 공개 클래스: FscFinancialCompanySource
- name = fsc_financial_company
- 생성자: Settings, count=2, 주입형 SupportsFetch, HostRateLimiters,
  SupportsCursorStore
- 메서드: applies_to(), discover(), _dry(), _client(), _fetch_page(),
  _item_label(), _company_from_item()

응답 모델은 Pydantic v2 내부 모델 _FscItem(BaseModel) 하나만 둔다. extra=ignore로
40개가 넘는 미사용 필드를 무시하고 실제로 읽는 필드만 alias로 선언한다. 응답의
response.header/body/items/item 봉투는 짧은 파서 함수로 푼다. item이 단건 객체 또는
배열인 변형을 모두 리스트로 정규화한다. 봉투마다 Pydantic 모델을 만드는 것은 재사용
가치가 없어 생략한다.

공식 샘플 응답을 fixture로 고정한 뒤 아래 의미 필드의 실제 JSON 키를 _FscItem alias로
확정한다. 현재 저장소에는 이 API fixture가 없으므로 필드명을 추측해 구현하지 않는다.

| API 의미 | DiscoveredCompany 매핑 | 규칙 |
|---|---|---|
| 회사명 (fncoNm) | name | 공백이면 행 제외 |
| 법인등록번호 (crno) | registry_id | 문자열 보존 |
| 사업자등록번호 | reg_no | 문자열 보존, 후단 dedup이 정규화 |
| 홈페이지 (fncoHmpgUrl) | domain | normalize_domain() 적용, ir_url 아님 |
| 주소 | address, 파생 region | build_company()가 KR 지역 파생 |
| 전화 | phone | 빈 문자열은 None |
| 표준산업분류 코드·명 | 업종 판정 입력 | 원문 필드는 저장하지 않음 |
| 상장일 | listed, listed_verified | 값이 있으면 listed/True, 없으면 unknown/False |

상장일 부재를 비상장으로 단정하지 않는다. 권위 있는 시장 필드가 없으므로 market=None,
IR 전용 URL이 아니므로 ir_url=None으로 둔다.

### 기존 파일 변경

leadcrawler/sources/registry.py에서 다음만 바꾼다.

1. FscFinancialCompanySource를 import한다.
2. DartSource 바로 뒤에 인스턴스를 추가하고 rate_limiters와 cursor_store를 전달한다.
3. KR 화이트리스트 허용 tuple을
   (FscFinancialCompanySource, NpsSource, NaverLocalSource)로 바꾼다.
4. kr_discovery_nps_only 설정명은 호환성을 위해 유지하고 주석만 “KR 승인 발견 소스
   화이트리스트”로 고친다.

config.py에는 새 필드를 추가하지 않는다. .env.example 수정도 실행 조건이 아니다. 기존
설정 키를 운영 문서에 노출해야 한다는 별도 요구가 생길 때 한 줄을 추가한다.

## 3. applies_to 게이팅

국가, 업종, 상장 조건을 모두 만족할 때만 적용한다.

### 국가와 지역

- is_country(segment, _KR)로 KR, KOR, Korea, 대한민국, 한국 별칭을 허용한다.
- region_aware를 선언하지 않는다. 레지스트리의 기존 지역 팬아웃 가드가 지역
  세그먼트에서 FSC를 제외하므로 전국 명부를 시·도마다 반복 호출하지 않는다
  (leadcrawler/sources/registry.py:169-174).

### 업종

허용 입력:

- 광범위: 전체, 금융, finance
- 닫힌 택소노미: 은행, 증권·자산운용, 보험, 연기금, 핀테크·결제
- 운영 alias: 증권, 증권사, 자산운용, 자산운용사, 투자자문, 투자자문사, 공제회, 연금

alias는 소스 내부의 작은 상수 dict로 닫힌 라벨에 정규화한다. 투자자문은
증권·자산운용, 공제회·연금은 연기금으로 귀속한다. 자유 텍스트나 비금융
택소노미에는 적용하지 않는다.

광범위 입력은 분류된 모든 금융회사와 미분류 행을 반환한다. 구체 입력은 해당 닫힌
라벨로 분류된 행만 반환한다. 산출 시 Segment.model_copy()로 업종을 닫힌 라벨로 바꿔
build_company()에 넘긴다. 투자자문 같은 검색 alias가 저장 업종으로 새지 않는다.

현재 택소노미에는 이미 은행, 증권·자산운용, 보험, 연기금, 핀테크·결제가 있다
(leadcrawler/sources/taxonomy.py:45-50). 따라서 투자자문 독립 택소노미는 신설하지 않는다.
기존 대분류에 합치는 것이 UI, 엑셀, 큐 필터의 라벨 파편화를 막는 최소 변경이다.

supported_industries()가 코드 매핑이 있는 라벨만 UI에 노출하는 현재 제약
(leadcrawler/sources/industry.py:291-304)은 이 소스 구현과 분리한다. CLI는 해당 라벨을
직접 받을 수 있고 운영 스크립트도 연기금,증권·자산운용을 사용한다. 관리자 UI에서 FSC
전용 금융 라벨 선택이 실제로 필요해질 때 노출 목록만 별도 변경한다.

### 상장 여부

- segment.listed == unknown: 상장·미확인 행 모두 허용한다.
- segment.listed == listed: 소스는 적용하되 상장일이 있는 행만 반환한다.
- segment.listed == unlisted: 적용하지 않는다. 상장일 부재가 비상장을 증명하지
  않기 때문이다.

## 4. 페이징과 커서

### 기본 알고리즘

요청 numOfRows=1000, resultType=json을 모듈 상수로 둔다. 페이지 크기는 설정으로 만들지
않는다. 다만 커서 산술에는 요청값을 그대로 믿지 않고 1페이지 body.numOfRows를 검증한
effective_page_size를 사용한다. 제공자가 1000을 더 작은 값으로 제한해도 pageNo와 skip이
틀어지지 않아야 한다.
basDt, crno, fncoNm은 전수 발견 경로에서 보내지 않는다. 이 필터들은 진단·재현용이지
모집단 순회를 위한 설정이 아니다.

1. 1페이지를 요청해 totalCount, body.numOfRows, 실제 items를 얻는다. body.numOfRows가
   1 이상 정수가 아니면 응답 실패로 처리한다.
2. cursor_offset(cursor_store, name, segment, totalCount)으로 원시 행 시작 offset을
   얻는다 (leadcrawler/sources/base.py:132-143).
3. pageNo = offset // effective_page_size + 1,
   skip = offset % effective_page_size를 계산한다. 시작 페이지가 1이면 이미 받은 응답을
   재사용하고, 아니면 해당 페이지부터 요청한다.
4. 시작 페이지에서 skip개를 건너뛴 뒤 행을 순서대로 분류한다. 분류 불가·다른 업종·이름
   누락 행도 “처리 완료 원시 행”으로 세어 offset을 전진한다.
5. 해당 세그먼트 산출이 settings.discovery_max_per_source에 도달하거나 모집단 끝에
   닿을 때까지 다음 페이지를 순차 요청한다.
6. 성공적으로 처리한 마지막 원시 위치를 advance_cursor()로 한 번 확정한다. 끝에 닿으면
   공용 헬퍼가 0으로 되돌린다 (leadcrawler/sources/base.py:145-163).

커서는 매칭 건수나 API 페이지 번호가 아니라 원시 행 offset이다. 그래야 특정 업종의
매칭률이 낮아도 다음 실행이 정확히 이어지고 cap이 페이지 중간에 걸려도 건너뛰지 않는다.

일 1회 갱신으로 실행 사이 목록 순서가 바뀌면 경계에서 일부 중복 또는 지연 발견이 생길 수
있다. 중복은 canonical key 원장이 제거하고, 누락 후보는 한 바퀴가 끝난 다음 회차에서
다시 만난다. 스냅샷 전체를 로컬 복제하거나 basDt별 커서를 만드는 것은 생략한다.

### DART 기능별 판단

| DART 기능 | FSC v1 | 판단 근거 |
|---|---|---|
| 목록+상세 2패스 | 제외 | 목록 한 응답에 홈페이지 포함 전 필드가 있음 |
| 다중 키 로테이션 | 제외 | 발급 키와 설정이 한 개뿐임 |
| 법인 상세 캐시 | 제외 | 상세 API N+1 호출이 없음 |
| 최근공시 우선 레인 | 제외 | 일 1회 스냅샷이며 실시간 우선 신호가 없음 |
| discover_chunks() | 제외 | 페이지 수가 작고 단일 커서 확정이 단순·안전함 |
| ThreadPoolExecutor | 제외 | 공용 레지스트리가 소스 간 병렬화함 |
| 런 간 커서 | 사용 | 기본 산출 cap 500 이후에도 목록 머리만 읽는 문제를 막음 |

> ponytail: 단일 순차 페이지 스캔. 전수 1회 시간이 잡 SLA를 넘는 실측이 생기면
> discover_chunks()로 범위를 나누고 finalize에서 커서를 한 번만 확정한다.

## 5. 업종 라벨 매핑

분류 우선순위는 “표준산업분류 코드+표준산업분류명 → 회사명 fallback → 미분류”다.
회사명 패턴은 코드·분류명이 없거나 모호할 때만 사용한다.

초기 규칙은 다음과 같이 좁게 둔다. 실제 leaf 코드와 API 필드명은 공식 샘플 fixture에서
확인한 값만 상수표에 넣고 광범위 64, 65, 66만으로 세부 라벨을 단정하지 않는다.

| 결과 라벨 | 코드/표준산업분류명 규칙 | 회사명 fallback |
|---|---|---|
| 은행 | 은행·저축기관으로 명시된 leaf 코드 또는 분류명 | 은행, 저축은행 |
| 보험 | 보험·재보험으로 명시된 leaf 코드 또는 분류명 | 보험, 생명, 화재, 손해보험 |
| 연기금 | 연금·기금 운용으로 명시된 leaf 코드 또는 분류명 | 연금공단, 연기금, 공제회 |
| 증권·자산운용 | 증권·선물중개·집합투자·자산운용·투자자문·투자일임 leaf 코드/분류명 | 증권, 자산운용, 투자자문, 투자일임 |
| 핀테크·결제 | 결제·전자금융으로 명시된 leaf 코드 또는 분류명 | 결제, 페이먼츠, 전자금융 |

규칙은 더 구체적인 연기금·보험·은행을 먼저, 증권·자산운용과 핀테크·결제를 뒤에
평가한다. 한 행이 둘 이상에 걸리거나 신뢰할 신호가 없으면 미분류다. 특정 업종
세그먼트는 이 행을 제외하고 전체·금융 세그먼트만 후단 분류 파이프라인에 넘긴다.

전역 industry_from_ksic()는 금융 64/65/66을 의도적으로 단일 라벨로 역매핑하지 않는다
(leadcrawler/sources/industry.py:437-443, tests/test_industry_classify.py:46-55). 이 정책을
깨지 않도록 FSC의 상세 코드·명칭 규칙은 새 모듈 안에 둔다. 검증되지 않은 금융 세부
KSIC 접두를 전역 _KSIC에 추가하지 않는다.

> ponytail: 투자자문은 기존 “증권·자산운용”에 합친다. 투자자문만 별도 큐·엑셀 필터로
> 운영해야 한다는 제품 요구와 실제 물량이 생기면 택소노미·UI·분류기까지 함께 분리한다.

## 6. dry-run 계약

discover()의 첫 분기는 settings.dry_run이어야 한다. 이 분기에서는 키 존재 여부를 보지
않고 _dry()만 호출하며 Fetcher, cursor store, quota 상태를 읽거나 쓰지 않는다.

- 대상 세그먼트마다 기본 2개를 반환한다.
- 이름, registry_id, 도메인, 주소, 전화는 입력 세그먼트와 index만으로 결정한다.
- registry=fsc, registry_id=fFSC{i:08d}로 canonical key를 안정화한다.
- 구체 업종 alias는 닫힌 택소노미 라벨로 정규화한다.
- 광범위 세그먼트는 고정 순서 은행, 증권·자산운용 더미를 반환한다.
- 더미 상장 상태는 세그먼트 계약을 보존하되 listed_verified=False다.

tests/conftest.py:48-69가 전체 테스트의 dry-run과 네트워크 차단을 강제하므로 라이브 파서
테스트는 Settings(dry_run=False)와 주입형 fake fetcher를 함께 사용한다.

## 7. 실패와 쿼터 처리

### 인증·응답 실패

- 라이브에서 service key가 비어 있으면 로그 한 줄 후 빈 목록을 반환하고 Fetcher를
  만들지 않는다.
- 공용 Fetcher의 HTTP 429/5xx 및 전송 오류 3회 재시도를 그대로 사용한다
  (leadcrawler/sources/http.py:124-157, 292-304). 중첩 재시도를 만들지 않는다.
- HTTP 200이어도 response.header.resultCode가 성공 코드가 아니면 해당 페이지에서
  중단하고 이전 성공 페이지의 결과만 반환한다.
- totalCount, body.numOfRows, items가 깨졌거나 모집단 중간에 빈 페이지 또는 설명되지
  않는 short page가 오면 실패 페이지 시작 위치 앞에 커서를 남긴다. malformed 개별
  행은 건너뛰되 처리 완료로 센다.
- 예외 문자열에는 query URL이 들어갈 수 있으므로 service key를 ***로 치환한 뒤
  기록한다. 같은 키를 쓰는 NPS CLI 선례가 있다 (leadcrawler/cli.py:509-512).

### 호출량

v1에는 DART식 영속 일일 카운터를 넣지 않는다.

- 최대 페이지 수는 첫 응답의 ceil(totalCount / numOfRows)로 닫혀 있다.
- 한 discover() 실행에서도 호출 수를 세어 10,000에 도달하기 전에 중단한다. 정상
  모집단에서 이 상한 도달은 데이터 또는 종료 조건 이상으로 경고한다.
- 같은 data_go_kr_service_key를 nps-sync도 쓰므로 FSC만의 영속 카운터는 실제 키 전체
  사용량을 정확히 나타내지 못한다 (leadcrawler/cli.py:485-563).

단일 키만 놓고 보면 SupportsCursorStore.increment()를 이용한 KST 날짜별 정수 카운터
하나면 충분하고 DART의 키 지문·로테이션은 필요 없다. 다만 실제 일 사용량이 8,000콜을
넘거나 FSC와 nps-sync가 같은 날 자주 겹친다는 관측이 생기면 두 호출부가 함께 쓰는
data_go_kr 공용 카운터로 승격해야 한다.

> ponytail: 부분적인 FSC 전용 일일 카운터는 생략한다. 공유 serviceKey 사용량이 80%를
> 넘는 운영 지표가 생기면 nps-sync와 함께 원자 increment하는 공용 원장으로 올린다.

## 8. 테스트 계획

신규 tests/test_fsc_financial_company.py에 fake fetcher와 dict cursor를 두고 검증한다.

### 게이팅과 dry-run

1. KR 별칭은 허용하고 US는 거부한다.
2. 금융 broad/닫힌 라벨/alias만 허용하고 건설은 거부한다.
3. unknown, listed는 적용하고 unlisted는 거부한다.
4. 같은 세그먼트를 두 번 호출한 결과와 canonical key가 완전히 같다.
5. BoomFetcher, BoomCursorStore를 주입해도 dry-run이 성공한다.
6. dry_run=False이고 키가 비면 네트워크 없이 빈 목록이다.

### 파싱과 필드 매핑

7. 공식 샘플을 줄인 fixture로 요청 URL과 serviceKey/pageNo/numOfRows/resultType을 검증한다.
8. items.item의 배열·단일 객체·빈 값 세 형태를 검증한다.
9. 40개 이상 extra 필드가 있어도 선택 필드만 파싱한다.
10. crno가 canonical key의 reg:fsc:를 만들고, 없으면 domain/name key로 폴백한다.
11. 홈페이지는 정규화한 domain으로 가고 ir_url은 비어 있다.
12. 주소에서 KR region이 파생되고 사업자번호·전화가 보존된다.
13. 상장일 있음은 listed/True, 없음은 unknown/False이며 market은 비어 있다.

### 업종

14. 코드·표준분류명 대표 fixture가 은행/보험/연기금/증권·자산운용/핀테크·결제로 매핑된다.
15. 투자자문 회사가 별도 자유 라벨이 아니라 증권·자산운용으로 저장된다.
16. 코드와 명칭이 충돌하면 미분류, 구체 세그먼트에서는 제외된다.
17. 회사명 fallback은 표준 코드·명칭이 없는 경우에만 작동한다.

### 페이지·커서·실패

18. 2개 이상 페이지를 순서대로 읽고 discovery_max_per_source에서 중단한다.
19. cap이 페이지 중간에 걸린 두 실행은 원시 registry ID가 겹치지 않는다.
20. 필터 불일치 행도 cursor offset을 전진시켜 다음 실행이 이어진다.
21. 모집단 끝에서 cursor가 0으로 돌아간다.
22. 중간 HTTP/API 오류는 이미 얻은 결과를 보존하되 실패 페이지를 건너뛰지 않는다.
23. 요청 numOfRows=1000보다 작은 body.numOfRows를 반환해도 응답 page size로 커서
    산술을 수행한다.
24. 비정상 totalCount/body.numOfRows, 빈 중간 페이지, nonterminal short page,
    malformed 행의 커서 동작을 고정한다.
25. 오류 로그 문자열에 service key가 남지 않는다.

기존 tests/test_sources.py에서는 등록 source name 집합, KR 기본 화이트리스트의 금융
세그먼트, 국가 alias, 전체 세그먼트 기대값을 갱신한다
(tests/test_sources.py:264-270, 592-636). 건설 세그먼트에서는 FSC가 적용되지 않으므로
기존 NPS+Naver 기대를 유지한다.

신규 테스트 안에서 build_sources(cursor_store=...)가 FSC에 store를 전달하는 것도
검증한다. 별도 cursor 테스트 파일을 늘리지 않는다.

검증 명령:

    ruff check leadcrawler/sources/fsc_financial_company.py leadcrawler/sources/registry.py \
      tests/test_fsc_financial_company.py tests/test_sources.py
    pytest -q tests/test_fsc_financial_company.py tests/test_sources.py
    pytest -q

전체 pytest는 저장소 운영 제약에 따라 라이브 키가 없는 체크아웃에서만 권위 있게 실행한다.

## 9. 예상 diff와 명시적 스킵

최소 구현 예상은 4파일, 순증 약 330-450줄이다.

| 파일 | 예상 변경 |
|---|---:|
| leadcrawler/sources/fsc_financial_company.py | 신규 170-220줄 |
| leadcrawler/sources/registry.py | 8-15줄 |
| tests/test_fsc_financial_company.py | 신규 140-190줄 |
| tests/test_sources.py | 12-25줄 |

kr_discovery_nps_only 화이트리스트 추가는 필수다. 빠지면 기본 설정에서 새 소스가
등록되어도 한 번도 실행되지 않는다.

v1에서 수정하지 않는 파일은 config.py, base.py, industry.py, taxonomy.py, 데이터베이스
schema와 migration이다. 새 의존성, 새 테이블, 새 CLI 명령도 없다.

> ponytail: DART 724줄의 운영 복잡도는 복사하지 않는다. 페이지 수·잡 시간이 실제
> 병목이 되거나 공유 serviceKey가 80% 이상 소진되는 증거가 생길 때만 청킹·공용 쿼터
> 원장을 추가한다.
