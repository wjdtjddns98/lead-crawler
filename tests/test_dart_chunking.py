"""DART 발견 chunking(A-refined) 단위 테스트 — fake-fetcher 로 live 경로 구동.

⚠️ dry_run 아님: dry_run 은 dart.py 에서 _dry() 로 단락돼 corps·청킹에 진입하지 않는다.
따라서 SupportsFetch 더블로 라이브(_live/_scan_range) 경로를 직접 구동한다.

- EP2(제약 ①): 청크 disjoint 구간 합집합 == 비청크 산출과 동일 집합(겹침/누락 0, 이중추출 0).
- EP3(백필+부분실패): (a) 전청크 완주 → 커서 window 만큼 1회 전진.
  (b) 중간청크 020 조기정지 → 커서 min-contiguous-frontier 로만 전진(뒤 완주청크 gap 스킵 0).
- EP4(fetch-once): N청크여도 corpCode.xml fetch 1회.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from leadcrawler.config import Settings
from leadcrawler.sources.base import Segment
from leadcrawler.sources.dart import _CHUNK_SCAN, _QUOTA_SOURCE, DartSource, _quota_key


class FakeCursorStore:
    """SupportsCursorStore 더블 — dict 저장(커서·일일예산 카운터 겸용, increment 지원)."""

    def __init__(self, initial: dict[tuple[str, str], int] | None = None) -> None:
        self.data: dict[tuple[str, str], int] = dict(initial or {})

    def get(self, source: str, key: str) -> int:
        return self.data.get((source, key), 0)

    def advance(self, source: str, key: str, position: int) -> None:
        self.data[(source, key)] = position

    def increment(self, source: str, key: str, delta: int) -> None:
        self.data[(source, key)] = self.data.get((source, key), 0) + delta


class CountingFetcher:
    """SupportsFetch 더블 — corpCode ZIP(get_bytes)과 company.json(get_json) 호출 계수."""

    def __init__(self, corps_n: int, *, on_company) -> None:
        self._corps_n = corps_n
        self._on_company = on_company
        self.zip_calls = 0
        self.company_codes: list[str] = []

    def get_bytes(self, url: str, *, params=None, headers=None) -> bytes:
        self.zip_calls += 1
        return _corp_zip(self._corps_n)

    def get_json(self, url: str, *, params=None, headers=None) -> Any:
        code = (params or {})["corp_code"]
        self.company_codes.append(code)
        return self._on_company(code)


def _corp_zip(n: int) -> bytes:
    """상장사 n 개짜리 corpCode.xml(ZIP) 더미 — corp_code = 00000001..n."""
    rows = "".join(
        f"<list><corp_code>{i:08d}</corp_code><corp_name>회사{i}</corp_name>"
        f"<stock_code>{i:06d}</stock_code></list>"
        for i in range(1, n + 1)
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", f"<result>{rows}</result>".encode())
    return buf.getvalue()


def _ok(code: str) -> dict:
    return {"status": "000", "corp_name": f"회사{code}", "hm_url": "", "corp_cls": "Y"}


def _settings(cap: int) -> Settings:
    # 로컬 .env 가 dart_api_key_2/_3 에 실키를 채우면 라운드로빈이 키를 분산시켜 쿼터
    # 단언이 깨진다 — 명시 빈값으로 단일 키("k")를 강제해 결정적으로 만든다.
    return Settings(
        dry_run=False,
        dart_api_key="k",
        dart_api_key_2="",
        dart_api_key_3="",
        dart_api_key_4="",
        discovery_max_per_source=cap,
    )


_SEG = Segment(country="KR", industry="전체")  # broad → 업종 필터 없음(scan_limit=cap).


def _run_chunks(src: DartSource, segment: Segment):
    """discover_chunks 를 registry 처럼 구동: 청크 순서대로 실행·수집 후 finalize."""
    chunks, finalize = src.discover_chunks(segment)
    results = [chunk() for chunk in chunks]
    finalize(results)
    return [c for chunk_out in results for c in chunk_out]


def test_ep2_chunk_union_equals_nonchunk_and_disjoint() -> None:
    """제약 ①: 청크 합집합 == 비청크 산출(동일 집합), 각 corp_code 정확히 1회만 스캔."""
    corps_n = 5 * _CHUNK_SCAN
    cap = 3 * _CHUNK_SCAN  # window [0, 3*chunk) → 3 청크(각 _CHUNK_SCAN).

    # 비청크 기준선(_live 직접) — 같은 모집단·cap.
    base_fetcher = CountingFetcher(corps_n, on_company=_ok)
    base = DartSource(_settings(cap), fetcher=base_fetcher)
    baseline = {d.registry_id for d in base.discover(_SEG)}
    assert len(baseline) == cap  # broad → window 전량 수집.

    # 청크 경로 — 동일 모집단·cap.
    chunk_fetcher = CountingFetcher(corps_n, on_company=_ok)
    src = DartSource(_settings(cap), fetcher=chunk_fetcher, cursor_store=FakeCursorStore())
    out = _run_chunks(src, _SEG)
    ids = [d.registry_id for d in out]

    assert set(ids) == baseline  # 동일 집합(경계 겹침/누락 0).
    assert len(ids) == len(set(ids))  # 이중추출 0.
    # corp_code 를 정확히 1회만 조회(disjoint 구간) — 이중 company.json 호출 없음.
    assert len(chunk_fetcher.company_codes) == cap
    assert len(set(chunk_fetcher.company_codes)) == cap


def test_ep2gap_dense_narrow_industry_trims_output_to_cap() -> None:
    """고밀도 좁은 업종(scan_limit=cap*10=5000, 10청크): 청크경로 output == cap(>cap 아님),
    _live 와 동일(corp 오름차순 첫 cap개). 콜수는 문서화된 한계(구간 전량 스캔)라 cap 초과 허용."""
    from leadcrawler.sources.dart import _SCAN_LIMIT_ABS

    corps_n = _SCAN_LIMIT_ABS  # window = min(cap*10, ABS) = 5000.
    cap = 500  # scan_limit = min(500*10, 5000) = 5000 → 10 청크.
    seg = Segment(country="KR", industry="반도체")  # 매핑 좁은 업종(KSIC 26x).

    def _match(code: str) -> dict:
        # 전부 반도체(induty 264) 매칭 = 고밀도 → _live 는 cap 에서 조기중단.
        return {
            "status": "000", "corp_name": f"회사{code}",
            "hm_url": "", "induty_code": "264", "corp_cls": "Y",
        }

    base = DartSource(_settings(cap), fetcher=CountingFetcher(corps_n, on_company=_match))
    baseline = [d.registry_id for d in base.discover(seg)]
    assert len(baseline) == cap  # _live: len(out) >= cap 조기중단.

    fetcher = CountingFetcher(corps_n, on_company=_match)
    src = DartSource(_settings(cap), fetcher=fetcher, cursor_store=FakeCursorStore())
    ids = [d.registry_id for d in _run_chunks(src, seg)]
    assert len(ids) == cap  # 청크경로도 cap 으로 trim(>cap 아님) — 출력계약 복원.
    assert ids == baseline  # corp 오름차순 첫 cap개 — _live 와 동일(결정적).
    # 문서화된 한계: 순수워커라 구간 전량 스캔 → company.json 콜수는 cap 초과(scan_limit=5000).
    assert len(fetcher.company_codes) > cap


def test_ep3a_all_chunks_complete_advances_cursor_by_window() -> None:
    """전청크 완주 → 커서가 window(=cap) 만큼 1회 전진(다음 런 frontier 이어감)."""
    corps_n = 5 * _CHUNK_SCAN
    cap = 3 * _CHUNK_SCAN
    store = FakeCursorStore()
    fetcher = CountingFetcher(corps_n, on_company=_ok)
    src = DartSource(_settings(cap), fetcher=fetcher, cursor_store=store)
    _run_chunks(src, _SEG)
    # window_end = offset(0) + cap < len(corps) → 커서 = cap(랩 아님).
    assert store.get("dart", _SEG.label) == cap
    # 쿼터: corpCode 1 + company.json cap.
    assert store.get(_QUOTA_SOURCE, _quota_key("k")) == cap + 1


def test_ep3b_mid_chunk_fatal_advances_min_contiguous_frontier() -> None:
    """중간청크 020 조기정지 → 커서 min-contiguous-frontier 로만 전진(뒤 완주청크 gap 스킵 0)."""
    corps_n = 5 * _CHUNK_SCAN
    cap = 3 * _CHUNK_SCAN
    # 청크1(구간 [chunk, 2*chunk))의 중간에서 020 주입 → stopped_at = fatal 위치.
    fatal_idx = _CHUNK_SCAN + 200  # 0-based scan index.
    fatal_code = f"{fatal_idx + 1:08d}"  # corps[idx] = i(idx+1) → corp_code.

    def _on_company(code: str) -> dict:
        if code == fatal_code:
            return {"status": "020", "message": "사용한도를 초과하였습니다."}
        return _ok(code)

    store = FakeCursorStore()
    fetcher = CountingFetcher(corps_n, on_company=_on_company)
    src = DartSource(_settings(cap), fetcher=fetcher, cursor_store=store)
    _run_chunks(src, _SEG)
    # 청크0 완주(→ _CHUNK_SCAN), 청크1 fatal_idx 에서 정지 → frontier = fatal_idx.
    # 청크2 는 완주해도 gap 방지로 건너뜀(커서에 반영 안 됨).
    assert store.get("dart", _SEG.label) == fatal_idx
    # 쿼터(정확 집계): corpCode 1 + 청크0 _CHUNK_SCAN + 청크1 (200+1 fatal) + 청크2 _CHUNK_SCAN.
    expected = 1 + _CHUNK_SCAN + (200 + 1) + _CHUNK_SCAN
    assert store.get(_QUOTA_SOURCE, _quota_key("k")) == expected


def test_ep4_fetch_corpcode_once_for_n_chunks() -> None:
    """N청크여도 corpCode.xml fetch 1회(ZIP 재다운로드 0)."""
    corps_n = 5 * _CHUNK_SCAN
    cap = 3 * _CHUNK_SCAN
    fetcher = CountingFetcher(corps_n, on_company=_ok)
    src = DartSource(_settings(cap), fetcher=fetcher, cursor_store=FakeCursorStore())
    chunks, finalize = src.discover_chunks(_SEG)
    assert len(chunks) == 3  # window(cap=3*chunk) / _CHUNK_SCAN.
    assert fetcher.zip_calls == 1  # fetch-once(청킹 전 메인스레드 1콜).
    [chunk() for chunk in chunks]
    assert fetcher.zip_calls == 1  # 청크 실행은 ZIP 재fetch 없음.


def test_quota_reservation_visible_before_scan_and_settled_after() -> None:
    """예산 선예약: discover_chunks 직후 카운터 = fetch(1) + 계획 창 — 형제 세그먼트가
    이 예약을 보고 자기 창을 줄인다(집단 초과구독 차단). finalize 정산 후 = 실사용."""
    corps_n = 5 * _CHUNK_SCAN
    cap = 3 * _CHUNK_SCAN  # window = 3*chunk.
    store = FakeCursorStore()
    fetcher = CountingFetcher(corps_n, on_company=_ok)
    src = DartSource(_settings(cap), fetcher=fetcher, cursor_store=store)

    chunks, finalize = src.discover_chunks(_SEG)
    # 예약 시점(스캔 전): corpCode 1 + 계획 창(cap) 이 이미 기록돼 있어야 한다.
    assert store.get(_QUOTA_SOURCE, _quota_key("k")) == cap + 1

    results = [chunk() for chunk in chunks]
    finalize(results)
    # 전청크 완주 → 실사용 == 계획 → 정산 후에도 cap + 1(변화 없음).
    assert store.get(_QUOTA_SOURCE, _quota_key("k")) == cap + 1


def test_quota_reservation_refunds_early_stop() -> None:
    """중간청크 020 조기중단 → 미사용 예약분이 환불돼 카운터 = 실사용 콜수."""
    corps_n = 5 * _CHUNK_SCAN
    cap = 3 * _CHUNK_SCAN
    fatal_idx = _CHUNK_SCAN + 200
    fatal_code = f"{fatal_idx + 1:08d}"

    def _on_company(code: str) -> dict:
        if code == fatal_code:
            return {"status": "020", "message": "사용한도를 초과하였습니다."}
        return _ok(code)

    store = FakeCursorStore()
    src = DartSource(
        _settings(cap), fetcher=CountingFetcher(corps_n, on_company=_on_company),
        cursor_store=store,
    )
    _run_chunks(src, _SEG)
    # 실사용: corpCode 1 + 청크0 완주 + 청크1 (200 + fatal 1콜) + 청크2 완주.
    expected = 1 + _CHUNK_SCAN + (200 + 1) + _CHUNK_SCAN
    assert store.get(_QUOTA_SOURCE, _quota_key("k")) == expected


def test_quota_sibling_segment_sees_reservation() -> None:
    """형제 세그먼트 시나리오: 예약이 걸린 상태에서 두 번째 discover_chunks 는 남은
    예산만큼만 창을 연다 — used0 스냅샷 집단 초과구독(2026-07-10 사고 증폭기) 차단."""
    corps_n = 20 * _CHUNK_SCAN
    cap = 3 * _CHUNK_SCAN  # 세그먼트당 계획 창 = 1500.
    budget = 2000  # 키 1개, 일일예산 2000.
    store = FakeCursorStore()

    def _src() -> DartSource:
        s = Settings(
            dry_run=False, dart_api_key="k", dart_api_key_2="", dart_api_key_3="",
            dart_api_key_4="", discovery_max_per_source=cap,
            dart_daily_call_budget=budget,
        )
        return DartSource(s, fetcher=CountingFetcher(corps_n, on_company=_ok), cursor_store=store)

    chunks1, fin1 = _src().discover_chunks(_SEG)  # 예약: 1(fetch) + 1500.
    seg2 = Segment(country="KR", industry="전체", listed="listed")  # 다른 라벨(독립 커서).
    chunks2, fin2 = _src().discover_chunks(seg2)
    # 두 번째 세그먼트의 창 = 잔여예산(2000-1501-1(fetch)) 안으로 클램프 → 계획 1500 이 아님.
    planned2 = sum(len(c()) for c in chunks2)  # broad 전체 매칭이라 산출수 == 스캔수.
    assert planned2 <= budget - 1501
