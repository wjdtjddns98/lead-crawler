"""컬럼 소급 백필 — 기존 행의 업종(industry)·상장/시장(listed/market)을 주입 함수로 채운다.

cli.py 에 살던 비즈니스 로직을 옮긴 것(2026-08-26 구조 감사 #1). 네트워크·분류기는 전부
주입(``fetch_html``/``get_info``/``classifier``)이라 테스트는 스텁으로 무네트워크 검증한다.
"""

from __future__ import annotations

import httpx
from sqlalchemy import and_, func, or_, select, update

from ..schema import CompanyRow, DiscoveredCompanyRow
from ..sources.base import english_display, needs_english_name
from ..sources.countries import country_match_set
from ..sources.dart import _LISTED_CLS, _MARKET_CLS
from ..sources.gleif import _API_URL as _GLEIF_URL
from ..sources.gleif import english_other_name
from ..sources.taxonomy import AMBIGUOUS_LABELS


def backfill_industries(
    session, classifier, *, fetch_html, limit: int = 0, commit_every: int = 50
) -> tuple[int, int]:
    """'미분류'·catch-all 구분의 실존 회사를 재분류해 갱신한다 — (검토, 갱신) 건수 반환.

    파이프라인 유입 시점과 같은 규칙(AMBIGUOUS_LABELS → 분류기, abstain=원래값 유지)을
    기존 행에 소급 적용한다. 홈페이지 본문(``fetch_html``)이 있을 때만 분류한다 — 없으면
    (홈페이지 없음·fetch 실패) 이름만 블라인드 분류(오라벨·과금)하지 않고 스킵한다
    (파이프라인 홈페이지 게이트와 동일 규칙). 닫힌 택소노미 밖 값은 절대 쓰지 않고
    abstain 은 원래값을 유지하므로 반복 실행해도 안전하다(멱등).

    ``commit_every`` 건마다 중간 커밋한다 — 전체 런은 행당 유료 호출이 있어, 중단 시
    전량 롤백이면 그만큼의 LLM 지출이 통째로 증발한다(0=끄기, 마지막 커밋은 호출부).
    """
    stmt = (
        select(CompanyRow, DiscoveredCompanyRow.domain)
        .join(
            DiscoveredCompanyRow,
            DiscoveredCompanyRow.canonical_key == CompanyRow.canonical_key,
        )
        .where(CompanyRow.is_active.is_(True), CompanyRow.industry.in_(AMBIGUOUS_LABELS))
        .order_by(CompanyRow.id)
    )
    if limit:
        stmt = stmt.limit(limit)
    rows = session.execute(stmt).all()
    updated = 0
    for i, (company, domain) in enumerate(rows, start=1):
        html = fetch_html(company.homepage) if company.homepage else None
        # 홈페이지 게이트(파이프라인 run.py 와 동일): 본문 없으면 이름만 블라인드 분류로
        # 오라벨(자동차 편중)·과금하지 않고 스킵 — 미분류 유지, 다음 실행에서 재시도(멱등).
        if not html:
            continue
        # 분류기는 계약상 실패를 abstain(None)으로 흡수한다 — 확신 라벨일 때만 갱신.
        verdict = classifier.classify(company.name, domain, html)
        if verdict.label and verdict.label != company.industry:
            company.industry = verdict.label
            updated += 1
        if commit_every and i % commit_every == 0:
            session.commit()  # 중단돼도 여기까지의 재분류(=지출)는 살린다.
    return len(rows), updated


def fetch_industry_html(url: str, *, get, render) -> str | None:
    """업종 백필용 홈페이지 본문 확보 — 폴백 사다리(www → http → 헤드리스). 실패=None.

    ``get(url)`` 은 httpx 응답(status_code·text), ``render(url)`` 은 헤드리스 렌더 HTML
    (실패 None)을 돌려주는 주입 함수(테스트=스텁). 2026-08-26 잔여 미분류 표본 40곳 실측:
    - www 폴백(기존): 루트 무응답·www 만 서빙하는 사이트 회수.
    - **http 폴백**: https 접속 자체가 실패(ConnectError — 인증서 만료·https 미서빙)한 호스트만
      http:// 로 1회 더 — ConnectError 12곳 중 3곳 회수. 4xx/5xx 응답을 받은 호스트는 안 한다.
    - **헤드리스**: 403 은 대부분 봇차단(Cloudflare 등)이라 실브라우저는 통과 — 9곳 중 6곳 회수.
      406(KR 호스팅 차원 차단)은 브라우저도 막혀 제외. 미통과 챌린지 페이지는 본문으로 안 친다.
    """
    scheme, sep, host = url.partition("://")
    if not sep:
        scheme, host = "https", url
    hosts = [host] if host.startswith("www.") else [host, f"www.{host}"]
    blocked_url: str | None = None  # 403 을 낸 실제 후보 — 헤드리스는 이 URL 을 렌더한다.
    for h in hosts:
        for s in dict.fromkeys((scheme, "http")):
            candidate = f"{s}://{h}"
            try:
                r = get(candidate)
            except (httpx.ConnectError, httpx.ConnectTimeout):  # 접속 자체 실패 → 다음 스킴.
                continue
            except Exception:  # 읽기 타임아웃 등 — 접속은 됐으니 http 로 또 기다리지 않는다.
                break
            if r.status_code < 400 and r.text:
                return r.text
            if r.status_code == 403 and blocked_url is None:
                blocked_url = candidate
            break  # 응답은 받았다(4xx/5xx) — 같은 호스트의 다른 스킴은 안 본다.
    if blocked_url:
        html = render(blocked_url) or ""
        low = html.lower()
        if html and "just a moment" not in low and "cf-chl" not in low:
            return html
    return None


def backfill_dart_markets(
    session, get_info, *, limit: int = 0, commit_every: int = 50
) -> tuple[int, int]:
    """DART 원장 행의 상장여부(listed)·시장 보드(market)를 corp_cls 로 소급 기입한다
    — (검토, 갱신) 건수 반환.

    대상: registry='dart' 이고 ① listed='unknown'(#130 corp_cls 세분화 이전 코드가 남긴
    잔재) 또는 ② listed='listed' 인데 market 미상. ``get_info(corp_code)`` 는 DART
    company.json 응답 dict(실패/미확인=None)를 돌려주는 주입 함수(테스트=스텁, 라이브=API).
    corp_cls 를 못 받으면 원래값을 유지하므로 반복 실행해도 안전하다(멱등).
    ``commit_every`` 건마다 중간 커밋한다(중단 시 진행분 보존, 0=끄기).
    """
    stmt = (
        select(DiscoveredCompanyRow)
        .where(
            DiscoveredCompanyRow.registry == "dart",
            DiscoveredCompanyRow.registry_id.is_not(None),
            or_(
                DiscoveredCompanyRow.listed == "unknown",
                and_(
                    DiscoveredCompanyRow.listed == "listed",
                    DiscoveredCompanyRow.market.is_(None),
                ),
            ),
        )
        .order_by(DiscoveredCompanyRow.canonical_key)
    )
    if limit:
        stmt = stmt.limit(limit)
    rows = session.scalars(stmt).all()
    updated = 0
    for i, row in enumerate(rows, start=1):
        info = get_info(row.registry_id)
        cls = (info or {}).get("corp_cls", "")
        new_listed = _LISTED_CLS.get(cls)
        if new_listed is None:  # 응답 실패/미지 corp_cls → 원래값 유지(멱등·보수적).
            continue
        new_market = _MARKET_CLS.get(cls)
        if (row.listed, row.market) != (new_listed, new_market):
            row.listed = new_listed
            row.market = new_market
            updated += 1
        if commit_every and i % commit_every == 0:
            session.commit()  # 중단돼도 여기까지의 기입(=API 콜 소비분)은 살린다.
    return len(rows), updated


def _replace_display_name(session, row: DiscoveredCompanyRow, eng: str) -> None:
    """원장 표시명을 영문으로 교체(원어는 name_eng 보관)하고, 승격된 company 도 같은 원어명이면
    함께 교체한다(수동 수정명은 원어와 달라 보존) — save_discovered #412 분기와 같은 규칙."""
    old = row.name
    row.name_eng = row.name_eng or old
    row.name = eng
    session.execute(
        update(CompanyRow)
        .where(CompanyRow.canonical_key == row.canonical_key, CompanyRow.name == old)
        .values(name=eng)
        .execution_options(synchronize_session=False)  # 스테일 평가 오염 방지(backfill_job 선례).
    )


def _english_targets(
    session, *, registry: str | None = None, with_domain: bool = False, limit: int = 0
) -> list[DiscoveredCompanyRow]:
    """영문 교체 대상 원장 행 — 원어 표시명·KR 제외(승격된 회사 우선). 원어 판정은 파이썬
    (PG/SQLite 공통·정규식 방언 회피)이라 SQL 은 KR·중복흡수·등록처·도메인만 먼저 거르고,
    스트리밍(yield_per)으로 훑다가 ``limit`` 에 닿으면 멈춘다(소량 시험이 전량 로드하지 않게)."""
    stmt = (
        select(DiscoveredCompanyRow)
        .outerjoin(CompanyRow, CompanyRow.canonical_key == DiscoveredCompanyRow.canonical_key)
        .where(
            DiscoveredCompanyRow.duplicate_of.is_(None),
            func.lower(DiscoveredCompanyRow.country).notin_(sorted(country_match_set(["KR"]))),
        )
        .order_by(CompanyRow.id.is_(None), DiscoveredCompanyRow.canonical_key)
        .execution_options(yield_per=500)
    )
    if registry:
        stmt = stmt.where(DiscoveredCompanyRow.registry == registry)
    if with_domain:
        stmt = stmt.where(func.coalesce(DiscoveredCompanyRow.domain, "") != "")
    out: list[DiscoveredCompanyRow] = []
    for r in session.execute(stmt).scalars():
        if needs_english_name(r.name, r.country or ""):
            out.append(r)
            if limit and len(out) >= limit:
                break
    return out


def backfill_name_eng(
    session, extractor, *, fetch_html, limit: int = 0, commit_every: int = 25
) -> tuple[int, int]:
    """원어 표시명(KR 제외)·도메인 보유 원장 행을 **홈페이지에 적힌** 영문 상호로 소급 교체 —
    (검토, 전환) 반환. 파이프라인 유입 시점(run._build_lead)과 같은 추출기·같은 규칙(abstain=
    원어 유지)이라 반복 실행해도 값은 안전하다.

    홈페이지가 abstain 이면 일본 사이트 관행인 ``/en/``·``/english/`` 를 한 번씩 더 본다.
    ponytail: abstain 행을 표시할 컬럼이 없어 재실행 시 같은 행에 다시 과금된다(행당 최대
    3콜·5원) — 이 CLI 는 수동·--limit 운영이라 허용. 반복 운영이 되면 name_eng_checked_at.
    """
    rows = _english_targets(session, with_domain=True, limit=limit)
    updated = 0
    for i, row in enumerate(rows, start=1):
        home = fetch_html(f"https://{row.domain}")
        if not home:
            continue  # 홈페이지 게이트(업종 백필과 동일) — 본문 없이 이름만으론 추측이라 스킵.
        eng = extractor.extract(row.name, row.domain, home)
        for path in ("/en/", "/english/"):
            if eng:
                break
            en_html = fetch_html(f"https://{row.domain}{path}")
            eng = extractor.extract(row.name, row.domain, en_html) if en_html else None
        if eng:
            _replace_display_name(session, row, eng)
            updated += 1
        if commit_every and i % commit_every == 0:
            session.commit()  # 중단돼도 여기까지의 전환(=지출)은 살린다.
    return len(rows), updated


def backfill_gleif_names(
    session, *, fetch_json, limit: int = 0, batch: int = 100, commit_every: int = 5
) -> tuple[int, int]:
    """GLEIF(registry=lei) 원어 표시명 행을 LEI 일괄 조회(``filter[lei]=a,b,…``, 무과금)로
    재조회해 등록자 제출 영문 법인명(otherNames en)으로 교체 — (검토, 전환) 반환.

    소스 수정(gleif.english_other_name) 이전에 적재된 행의 소급 경로. 도메인이 없어 홈페이지
    추출이 불가한 행(JP 3,983 등)은 이 경로만 유효하다. ``fetch_json(url, params)`` 주입.
    """
    rows = _english_targets(session, registry="lei", limit=limit)
    by_lei = {r.registry_id: r for r in rows if r.registry_id}
    leis = list(by_lei)
    updated = 0
    for n, start in enumerate(range(0, len(leis), batch), start=1):
        chunk = leis[start:start + batch]
        payload = fetch_json(
            _GLEIF_URL, {"filter[lei]": ",".join(chunk), "page[size]": len(chunk)}
        )
        for rec in (payload.get("data") if isinstance(payload, dict) else None) or []:
            row = by_lei.get(str(rec.get("id") or ""))
            entity = (rec.get("attributes") or {}).get("entity") if isinstance(rec, dict) else None
            if row is None or not isinstance(entity, dict):
                continue
            name, _ = english_display(row.name, english_other_name(entity), row.country or "")
            if name != row.name:
                _replace_display_name(session, row, name)
                updated += 1
        if commit_every and n % commit_every == 0:
            session.commit()
    return len(rows), updated
