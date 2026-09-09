# 1회성(2026-08-21, git 미반영): 국가 빈값 LLM 백필 — 결정적 신호 소진 후 잔여분(PO 지시).
"""국가가 비어 있고 도메인만 있는 행의 국가를 홈페이지 본문으로 LLM 판별한다.

industry_classify.ClaudeClassifier 와 동일 규율:
  · 본문 있을 때만 판별(블라인드 금지 — fetch 실패는 스킵·무과금·빈값 유지)
  · 닫힌 ISO2 집합 밖 답·ABSTAIN 은 갱신 안 함(멱등·재실행 안전)
  · 콜 캡 + 월예산 가드(cost_ledger) 그대로

사용: python scripts/backfill_country_llm.py [--limit 0] [--commit-every 50]
"""

from __future__ import annotations

import argparse
import time

import httpx
from sqlalchemy import text as sqltext

from leadcrawler.config import Settings
from leadcrawler.cost_ledger import CostLedger
from leadcrawler.enrich.industry_classify import PROVIDER, _text_from_html
from leadcrawler.storage.db import get_sessionmaker

# 허용 ISO2 닫힌집합 — 레지스트리 등록국 + ccTLD 매핑 국가(추측 신국가 차단).
_ISO2 = {
    "US", "KR", "JP", "CN", "PH", "TH", "ID", "MY", "SG", "VN", "IN", "TW", "HK", "GB",
    "DE", "FR", "AU", "CA", "BR", "NZ", "TR", "MX", "IT", "FI", "ES", "NL", "BE", "PL",
    "CZ", "CH", "NO", "SE", "HU", "MN", "DK", "IE", "AT", "BD", "SA", "AE", "KW", "BH",
    "QA", "JO", "OM", "PT", "EG", "LB", "EE", "PS", "GR", "IL", "LT", "IQ", "RO", "RS",
    "KH", "LU", "SI", "IS", "HR", "LV", "BN", "MT", "MM", "AO", "TL", "CY", "SK", "UA",
    "KZ", "RU", "ZA", "AR", "CL", "CO", "PE", "NG", "KE", "PK", "LK", "NP", "UZ",
}

_PROMPT = """다음 회사의 본사 소재 국가를 판별하라. 회사명·도메인·홈페이지 본문을 근거로만 판단하고,
확신이 없으면 ABSTAIN 이라고만 답하라. 답은 ISO 3166-1 alpha-2 코드(예: KR, US, DE) 또는 ABSTAIN
한 단어만 출력한다.

회사명: {name}
도메인: {domain}
홈페이지 본문 발췌:
{text}
"""


def _fetch(client: httpx.Client, domain: str) -> str | None:
    for url in (f"https://{domain}", f"https://www.{domain}"):
        try:
            r = client.get(url, follow_redirects=True, timeout=8.0)
            if r.status_code < 400 and r.text:
                return r.text
        except Exception:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--commit-every", type=int, default=50)
    args = ap.parse_args()

    settings = Settings(industry_llm_max_calls=10_000)
    if settings.dry_run:
        print("DRY_RUN=true — 과금 백필 무의미. 중단.", flush=True)
        return 1
    auth = settings.anthropic_auth_token or settings.anthropic_api_key
    if not auth:
        print("anthropic 인증정보 없음 — 중단.", flush=True)
        return 1
    ledger = CostLedger(settings, persist=True)

    import anthropic

    if settings.anthropic_auth_token:
        client_llm = anthropic.Anthropic(auth_token=settings.anthropic_auth_token, max_retries=2)
    else:
        client_llm = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=2)

    sm = get_sessionmaker(settings)
    with sm() as s:
        rows = s.execute(sqltext(
            "select d.canonical_key, d.name, d.domain from discovered_company d"
            " where (coalesce(d.country,'')='' or d.country ~ '^[0-9?]+$')"
            "   and coalesce(d.domain,'') <> '' order by d.canonical_key"
            + (f" limit {int(args.limit)}" if args.limit else "")
        )).all()
        print(f"[country-llm] 대상 {len(rows)}", flush=True)
        done = filled = fetch_fail = abstain = 0
        t0 = time.monotonic()
        with httpx.Client(headers={"User-Agent": "Mozilla/5.0 (lead-crawler country-backfill)"}) as http:
            for r in rows:
                done += 1
                html = _fetch(http, r.domain)
                if not html:
                    fetch_fail += 1
                    continue
                if ledger.is_over_budget():
                    print("[country-llm] 예산가드 차단 — 중단", flush=True)
                    break
                try:
                    msg = client_llm.messages.create(
                        model=settings.industry_llm_model, max_tokens=8,
                        messages=[{"role": "user", "content": _PROMPT.format(
                            name=r.name or "(미상)", domain=r.domain,
                            text=_text_from_html(html) or "(없음)",
                        )}],
                    )
                    ledger.record(PROVIDER)
                    out = "".join(
                        b.text for b in msg.content if getattr(b, "type", None) == "text"
                    ).strip().upper()
                except Exception as exc:
                    print(f"[country-llm] LLM 오류 {r.domain}: {exc}", flush=True)
                    continue
                if out not in _ISO2:
                    abstain += 1
                    continue
                s.execute(sqltext(
                    "update discovered_company set country=:c where canonical_key=:k"
                ), {"c": out, "k": r.canonical_key})
                s.execute(sqltext(
                    "update company set country=:c where canonical_key=:k"
                    " and (coalesce(country,'')='' or country ~ '^[0-9?]+$')"
                ), {"c": out, "k": r.canonical_key})
                filled += 1
                if args.commit_every and filled % args.commit_every == 0:
                    s.commit()
                if done % 100 == 0:
                    rate = done / (time.monotonic() - t0)
                    print(f"[country-llm] {done}/{len(rows)} 채움 {filled}"
                          f" fetch실패 {fetch_fail} abstain {abstain} {rate:.1f}/s", flush=True)
        s.commit()
        print(f"[country-llm] 완료 — 처리 {done} 채움 {filled} fetch실패 {fetch_fail}"
              f" abstain {abstain}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
