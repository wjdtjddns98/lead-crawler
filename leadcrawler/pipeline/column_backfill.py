"""컬럼 소급 백필 — 기존 행의 업종(industry)·상장/시장(listed/market)을 주입 함수로 채운다.

cli.py 에 살던 비즈니스 로직을 옮긴 것(2026-08-26 구조 감사 #1). 네트워크·분류기는 전부
주입(``fetch_html``/``get_info``/``classifier``)이라 테스트는 스텁으로 무네트워크 검증한다.
"""

from __future__ import annotations

import httpx
from sqlalchemy import and_, or_, select

from ..schema import CompanyRow, DiscoveredCompanyRow
from ..sources.dart import _LISTED_CLS, _MARKET_CLS
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
