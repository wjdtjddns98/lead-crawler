"""EDINET 코드리스트 발견 소스 — 일본 상장사 전수 등록처(금융청 공시시스템).

금융청 EDINET 이 일 단위로 게시하는 제출자 코드리스트(``Edinetcode.zip``)를 내려받아
상장(上場) 내국법인을 열거한다(2026-08-25 실측: 전체 11,384행 중 상장 3,822사 — JPX
상장사 모집단 전수에 해당). 무인증·무WAF·고정 URL·ZIP+CSV(cp932)라 stdlib 만으로
파싱한다(새 의존성 0).

- 산출 필드: EDINET코드(``reg:edinet:<코드>``)·일문 상호·영문명(90% 보유 — resolve 의
  name_eng 폴백 연료)·주소·TSE 33업종(택소노미 매핑)·증권코드(ticker)·법인번호(reg_no).
  **도메인은 없음** — 기존 resolve_domains(opt-in) 경로가 영문명으로 해석한다(소스 측
  추가 코드 0). 시장 보드(프라임/스탠다드/그로스)는 코드리스트에 없어 market=None.
- 업종: TSE 33업종 중 명확 단일매치만 라벨링, 모호분(情報・通信業·サービス業 등)은
  None → '미분류'(LLM 배치 후속). 구체 업종 세그먼트는 매핑 일치 행만(순도 우선 —
  은행이 타 라벨로 오라벨되는 실사고 계열 방지).
- 커서: 단일 GET 으로 전 모집단이 오므로(570KB) 페이징 대신 **로컬 슬라이스** —
  ``base.cursor_offset/advance_cursor`` 재사용(키=segment.label, 소진 시 0 리셋).
  네트워크는 **인스턴스당 1콜**(메모) — 병렬 발견은 워커별 인스턴스라 워커 수만큼.
- 실존: 말소·비상장 제출자는 上場区分 필터로 제외, 外国法人・組合 은 국가 오분류
  방지를 위해 제외(SGX ADR 제외와 동일 논리). 하류 실존 게이트(제약②)가 백스톱.
- dry_run 은 네트워크 없이 결정적 더미(전 소스 계약).

ponytail: DART 식 2패스·키 로테이션·청킹은 없다(1콜 소스) — 코드리스트가 커지거나
EDINET API(키 필요)로 확장할 증거가 생기면 재검토. gBizINFO URL 백필(무료 토큰)은
resolve miss 가 문제 되면 추가(그때를 위해 reg_no 를 지금 저장한다).
"""

from __future__ import annotations

import csv
import io
import zipfile

from ..config import Settings
from ..logging import get_logger
from .base import (
    DiscoveredCompany,
    Segment,
    SupportsCursorStore,
    advance_cursor,
    build_company,
    cursor_offset,
    opt_str,
)
from .countries import resolve_country
from .http import Fetcher, HostRateLimiters, SupportsFetch
from .industry import is_broad_industry, resolve_industry_label

log = get_logger("sources.edinet")

_CODELIST_URL = (
    "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
)

# CSV 실측 헤더(2026-08-25) 중 사용하는 열들 — extra 열은 무시.
_COL_CODE = "ＥＤＩＮＥＴコード"
_COL_KIND = "提出者種別"
_COL_LISTED = "上場区分"
_COL_NAME = "提出者名"
_COL_NAME_ENG = "提出者名（英字）"
_COL_ADDR = "所在地"
_COL_SECTOR = "提出者業種"
_COL_SEC_CODE = "証券コード"
_COL_CORP_NO = "提出者法人番号"

_LISTED_MARK = "上場"
_DOMESTIC_KIND = "内国法人・組合"
# 헤더 검증 대상 — 필터 판정에 쓰는 핵심 열(하나라도 개칭되면 침묵 0건이 되므로).
_REQUIRED_COLS = (_COL_CODE, _COL_KIND, _COL_LISTED, _COL_NAME)

# TSE 33업종 → INDUSTRY_TAXONOMY. 명확 단일매치만 — 모호분은 None(미분류→LLM 후속).
# 情報・通信業(618)을 'IT·소프트웨어'로 강제하면 통신·방송사가 오라벨된다(닫힌 택소노미
# 파편화 실사고 계열)라 배제. 매핑은 2026-08-25 설계 검토에서 확정.
_SECTOR_TAXO: dict[str, str | None] = {
    "水産・農林業": "농림·수산",
    "建設業": "건설·엔지니어링",
    "食料品": "식품·음료",
    "繊維製品": "섬유·의류·패션",
    "化学": "화학·석유화학",
    "石油・石炭製品": "화학·석유화학",
    "医薬品": "제약·바이오",
    "ガラス・土石製品": "건축자재",
    "鉄鋼": "철강·금속",
    "非鉄金属": "철강·금속",
    "金属製品": "철강·금속",
    "機械": "기계·산업장비",
    # 電気機器·輸送用機器 는 TSE 기준 광의 버킷(반도체장비·항공철도 포함)이지만 다수가
    # 인접 정라벨이라 유지한다 — None 으로 낮추면 상장사 400+ 가 도메인 없는 상태로
    # 미분류에 쌓여 LLM 재분류(홈페이지 필요)도 못 받는다(리뷰 MED 트레이드오프 결정).
    "電気機器": "전자·전기부품",
    "輸送用機器": "자동차·모빌리티",
    "電気・ガス業": "에너지·전력",
    "陸運業": "물류·운송",
    "海運業": "물류·운송",
    "倉庫・運輸関連業": "물류·운송",
    "空運業": "여행·숙박·항공",
    "卸売業": "유통·도소매",
    "小売業": "유통·도소매",
    "不動産業": "부동산·개발",
    "パルプ・紙": "기타 제조",
    "ゴム製品": "기타 제조",
    "その他製品": "기타 제조",
    "精密機器": "기타 제조",
    "銀行業": "은행",
    "証券、商品先物取引業": "증권·자산운용",
    "保険業": "보험",
    "鉱業": "광업·자원",
    "情報・通信業": None,
    "サービス業": None,
    "その他金融業": None,
}
# applies_to 게이트 — 이 소스가 순도 있게 공급 가능한 구체 업종 라벨 집합.
_JP_LABELS = frozenset(v for v in _SECTOR_TAXO.values() if v is not None)


def _parse_codelist(blob: bytes) -> list[dict[str, str]]:
    """Edinetcode.zip → 행 dict 목록. 1행(메타, 헤더와 열수 불일치 quirk) 스킵.

    깨진 ZIP/CSV 는 빈 목록(graceful — DART corpcode 오류 처리 동형).
    """
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            # 멤버가 여럿이면 코드리스트 본체(Edinetcode*) 우선 — 순서 의존 제거(리뷰 MED).
            csv_names.sort(key=lambda n: (0 if "edinetcode" in n.lower() else 1, n))
            if not csv_names:
                return []
            text = zf.read(csv_names[0]).decode("cp932", errors="replace")
    except Exception as exc:
        log.info("edinet.zip_error", err=str(exc))
        return []
    lines = text.splitlines()
    if len(lines) < 2:
        return []
    reader = csv.reader(lines[1:])  # 0행=다운로드 메타 → 1행이 실제 헤더.
    try:
        header = next(reader)
    except StopIteration:
        return []
    missing = [c for c in _REQUIRED_COLS if c not in header]
    if missing:
        # 메타 행 부재·헤더 개편 등 상류 포맷 변경 — 필터 핵심 열(코드·종별·상장·상호)
        # 중 하나라도 빠지면 전 행이 조용히 탈락하므로 경고 후 중단(2차 리뷰 M1).
        log.warning("edinet.header_mismatch", missing=missing)
        return []
    out: list[dict[str, str]] = []
    for row in reader:
        if not row:
            continue
        out.append({h: (row[i] if i < len(row) else "") for i, h in enumerate(header)})
    return out


def _ticker(sec_code: str | None) -> str | None:
    """증권코드 5자(4자+체크자리) → 종목코드 4자('13760'→'1376', '130A0'→'130A').

    실데이터는 5자 또는 공란뿐(2026-08-25 실측) — 그 외 길이는 오염값으로 보고 버린다.
    """
    code = (sec_code or "").strip()
    if len(code) == 5:
        return code[:4]
    return code if len(code) == 4 else None


class EdinetSource:
    """EDINET 코드리스트 기반 일본 상장사 발견 소스(등록처 tier)."""

    name = "edinet"

    def __init__(
        self,
        settings: Settings,
        *,
        count: int = 2,
        fetcher: SupportsFetch | None = None,
        rate_limiters: HostRateLimiters | None = None,
        cursor_store: SupportsCursorStore | None = None,
    ) -> None:
        self._settings = settings
        self._count = count
        self._fetcher = fetcher
        self._rate_limiters = rate_limiters
        self._cursor_store = cursor_store
        # 인스턴스당 fetch-once(병렬 발견은 워커별 인스턴스라 최대 워커 수만큼 fetch).
        self._rows_memo: list[dict[str, str]] | None = None
        self._warned_no_resolver = False

    def applies_to(self, segment: Segment) -> bool:
        """JP 전용 + (broad 또는 매핑 가능 구체 업종). 비매핑 업종은 전 행이 필터에서
        떨어져 왕복만 낭비하므로 게이트에서 자른다(FSC _FIN_LABELS 동형)."""
        country = resolve_country(segment.country)
        if country is None or country.iso2 != "JP":
            return False
        if is_broad_industry(segment.industry):
            return True
        return resolve_industry_label(segment.industry) in _JP_LABELS

    def discover(self, segment: Segment) -> list[DiscoveredCompany]:
        """세그먼트에 해당하는 일본 상장사 목록을 반환한다."""
        if self._settings.dry_run:
            return self._dry(segment)
        return self._live(segment)

    def _dry(self, segment: Segment) -> list[DiscoveredCompany]:
        """네트워크 없는 결정적 더미(registry_id 기반 canonical_key, listed 규약)."""
        listed_seg = Segment(
            country=segment.country, industry=segment.industry, listed="listed"
        )
        return [
            build_company(
                source=self.name,
                segment=listed_seg,
                name=f"{segment.industry} EDINET Co {i}",
                # 라이브 레코드엔 도메인이 없지만 dry 더미는 '실존 active 기업' 시뮬레이션을
                # 위해 도메인을 부여한다(GLEIF gleif.py:83 명문 규약 — dry 전부 active).
                domain=f"jp-edinet{i}.co.jp",
                registry="edinet",
                registry_id=f"E{i:05d}",
                listed_verified=True,
            )
            for i in range(self._count)
        ]

    def _client(self) -> SupportsFetch:
        # 소스 인스턴스당 1개만 생성·재사용(discover 호출마다 클라이언트 누수 방지).
        if self._fetcher is None:
            self._fetcher = Fetcher(
                user_agent=self._settings.discovery_user_agent,
                min_interval=self._settings.http_request_delay,
                timeout=self._settings.http_timeout,
                rate_limiters=self._rate_limiters,
            )
        return self._fetcher

    def _rows(self) -> list[dict[str, str]]:
        """코드리스트 원시 행 — 런 내 1회만 내려받아 메모(다중 세그먼트 재fetch 방지).

        fetch 실패는 **메모하지 않는다** — 빈 결과를 고착하면 첫 타임아웃 1번이 런의
        JP 발견 전량을 침묵 0건으로 만든다(리뷰 HIGH — DART 는 세그먼트마다 재시도).
        """
        if self._rows_memo is None:
            try:
                blob = self._client().get_bytes(_CODELIST_URL)
            except Exception as exc:  # 네트워크/차단 → 이번 세그먼트만 빈 결과(재시도 가능).
                log.info("edinet.fetch_error", err=str(exc))
                return []
            rows = _parse_codelist(blob)
            if not rows:
                # 200 + 쓰레기 본문(CDN 오류 페이지 등)도 고착시키지 않는다 — 전송 실패와
                # 동일하게 다음 세그먼트에서 재시도(2차 리뷰 m1, 침묵 고착 방지).
                log.warning("edinet.parse_empty")
                return []
            if len(rows) < 1000:
                # 정상 코드리스트는 1만 행 이상(2026-08-25 실측 11,384) — 급감은 파싱
                # 붕괴(따옴표 불균형 흡수 등)나 상류 이변 신호(침묵 유실 방지).
                log.warning("edinet.parse_suspect", n=len(rows))
            self._rows_memo = rows
        return self._rows_memo

    def _live(self, segment: Segment) -> list[DiscoveredCompany]:
        """상장 내국법인 필터 → (구체 업종이면 매핑 일치) → 커서 슬라이스 → 캡."""
        cap = self._settings.discovery_max_per_source
        if cap <= 0:
            return []  # 0/음수 캡 설정에서 1건 누수 방지(리뷰 LOW).
        if not self._settings.resolve_domains and not self._warned_no_resolver:
            # 이 소스는 도메인을 못 준다 — resolve_domains 없이는 enrich·교차키 dedup 이
            # 전부 무력화된다(운영 계약). 침묵 대신 경고 — 인스턴스당 1회(스팸 방지).
            self._warned_no_resolver = True
            log.warning("edinet.no_resolver", hint="JP 크롤은 resolve_domains=true 권장")
        want: str | None = None
        if not is_broad_industry(segment.industry):
            want = resolve_industry_label(segment.industry)
        pool: list[dict[str, str]] = []
        for rec in self._rows():
            if rec.get(_COL_LISTED, "").strip() != _LISTED_MARK:
                continue
            if rec.get(_COL_KIND, "").strip() != _DOMESTIC_KIND:
                continue  # 外国法人 — JP 로 저장하면 국가 오분류(SGX ADR 제외 동형).
            if want is not None and _SECTOR_TAXO.get(rec.get(_COL_SECTOR, "").strip()) != want:
                continue
            pool.append(rec)

        # 커서 키 = segment.label(업종별 풀이 달라 라벨 단위가 정확). listed 파티션이
        # 키에 섞이는 트레이드오프는 FSC/GLEIF 와 동일 계열 — 호출부가 런당 단일 listed
        # 값이라 실해 없음(리뷰 LOW 기록).
        start = cursor_offset(self._cursor_store, self.name, segment, len(pool))
        out: list[DiscoveredCompany] = []
        pos = start
        for rec in pool[start:]:
            dc = self._candidate(segment, rec)
            pos += 1
            if dc is not None:
                out.append(dc)
                if len(out) >= cap:
                    break
        advance_cursor(self._cursor_store, self.name, segment, pos, len(pool))
        log.info("edinet.live", segment=segment.label, pool=len(pool), n=len(out), start=start)
        return out

    def _candidate(self, segment: Segment, rec: dict[str, str]) -> DiscoveredCompany | None:
        """레코드 1건 → DiscoveredCompany(형식 불일치는 제외).

        표시명은 **공식 영문 상호 우선**(검증 UI·엑셀 담당자가 일문을 못 읽는 운영 요구,
        2026-08-26 PO 결정 — 기존 행도 같은 규칙으로 일괄 전환됨). 일문 상호는 name_eng
        자리에 보관한다(필드명과 어긋나지만 원문 보존·재식별용 — 스키마 증설 없이).
        영문명 없는 ~10% 는 일문 그대로.
        """
        jp_name = opt_str(rec.get(_COL_NAME))
        eng_name = opt_str(rec.get(_COL_NAME_ENG))
        code = opt_str(rec.get(_COL_CODE))
        if not jp_name or not code:
            return None
        listed_seg = Segment(
            country=segment.country, industry=segment.industry, listed="listed"
        )
        return build_company(
            source=self.name,
            segment=listed_seg,
            name=eng_name or jp_name,
            domain=None,  # 코드리스트에 URL 없음 — resolve_domains 가 영문명으로 해석.
            registry="edinet",
            registry_id=code,
            industry_code_label=_SECTOR_TAXO.get(rec.get(_COL_SECTOR, "").strip()),
            address=opt_str(rec.get(_COL_ADDR)),
            reg_no=opt_str(rec.get(_COL_CORP_NO)),
            ticker=_ticker(rec.get(_COL_SEC_CODE)),
            name_eng=jp_name if eng_name else None,
            listed_verified=True,  # 上場区分 필터 통과 — 상장은 항상 실측값.
        )
