# 1회성(2026-09-01, git 미반영): KR 자산운용사 company 중복 행 병합(PO 지시 "③ 중복 병합부터").
"""공식 dedup(dedup_resolve.golden)은 발견원장의 duplicate_of 만 기록하고 company/큐 행은
안 건드린다 → 큐·엑셀의 중복은 그대로. 여기서 **company 단위**로 합친다(명시 그룹만).

그룹 = 사이트 제목·FINE 등록 홈페이지로 같은 회사임을 확인한 것만(추측 금지). 생존자 =
reg:dart > reg:fsc > dom: > name: 키 우선, 동률이면 연락처 많은 쪽, 그다음 id 사전순.
흡수 행 처리: ① 연락처를 생존자 id 로 재생성(contact id 는 (company,type,value) 결정적 해시라
UPDATE 대신 복제)+email_validation 복제 ② 큐: 후보 합집합, 상태는 **확정(confirmed)이 어느 쪽에든
있으면 그 사람 판단을 생존자에 승계**(리드손실 방지), 아니면 생존자 것 유지 ③ 발견원장 흡수행에
duplicate_of/merged_* 기록(공식 골든레코드 그래프와 동일 표기) ④ 흡수 company 행 삭제(FK CASCADE).
``move_contacts=False`` 그룹은 흡수행 도메인이 엉뚱한 사이트(오해석)라 연락처를 옮기지 않는다.

사용: python scripts/merge_dup_am_oneoff.py [--apply]
ponytail: promote 대상 SQL 이 duplicate_of 를 제외하지 않아, 흡수행 도메인이 생존자와 다르면
나중에 재승격될 수 있음(도메인 가드는 같은 도메인만 차단) — 별도 1줄 PR 후보로 보고.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from sqlalchemy import select

from leadcrawler.config import Settings
from leadcrawler.schema import CompanyRow, ContactRow, DiscoveredCompanyRow, EmailValidationRow
from leadcrawler.storage.db import get_sessionmaker
from leadcrawler.storage.repository import contact_id_for
from leadcrawler.storage.review import (
    ReviewQueueRow,
    candidate_values_of,
    enqueue_email_review,
    review_id_for,
)

# (회사 id 목록, 연락처 이동 여부, 생존자 홈페이지를 흡수행 것으로 교체 여부)
GROUPS: list[tuple[str, list[str], bool, bool]] = [
    ("어반자산운용", ["c_7fd63ef297c2b476921cdc2e188d09", "c_53dd3b4b0b0de3b7c4aac9e6242102"], True, False),
    ("태성자산운용", ["c_783552a91c6a7a26a422492721dda1", "c_bdef0a72da04a5a31e7e8cbb0e6891"], True, False),
    ("황소자산운용", ["c_d0687b004235a71cee5ad430716fa3", "c_af7e8ba6804822b4e106f4301a6b10"], True, False),
    ("라이언자산운용", ["c_fa0d1bfec5abff93bea9b329b66fa5", "c_0a2a31ed6827dbc24604cc33f8aeed"], True, False),
    ("스팍스자산운용", ["c_e0472a53548fa25c22a43095b799a6", "c_5fbee7e3d1b51db792d5ef84ba6920"], True, False),
    ("오르카자산운용", ["c_8b1f113fd940d3c345b8b670077631", "c_330b814a51cd214b64dcd951a1de5e"], True, False),
    # k-amc.com 행(c_1dcc7c…)은 근거 없어 제외.
    ("코리아자산운용", ["c_cbd184cf32faae9dcafe4d46240895", "c_473bc17f3f3e47da507e40e6b86b6f"], True, False),
    ("토러스자산운용", ["c_fc7c83c6fcea7c6ae66e99247c4f76", "c_12e9a31ee8e18d87fcaa6105b27fac"], True, False),
    ("엔라이튼자산운용", ["c_aa9d488b832335cb0b7a1a86710153", "c_37c9cff0f58b108358211f091351ee"], True, False),
    ("브이아이피자산운용", ["c_4ef79005a3f9f8624f96d5541d1f8d", "c_dac9acafaf80d5f6182f49d95a9a53"], True, False),
    ("타임폴리오자산운용", ["c_b226bb518ca52bc16cf587879f10a5", "c_83caaf41fd17b13ab186af0ccfcbc7",
                     "c_89c11128c6e621b0337012ef7894e5"], True, False),
    ("얼라인파트너스자산운용", ["c_900fa392a7bfeebcc230222761f8f4", "c_01aea7247e91d6698b2e5ef6e1dd92"], True, False),
    ("파인앤파트너스자산운용", ["c_7155d801383f23a43bc74b322f876c", "c_aec7d8336302492717c8d63d78b230"], True, False),
    ("현대자산운용", ["c_23a203da6620a15c7b8970dad4ea8e", "c_b2e0e3d98bbfc254cbaef64706b0fd"], True, False),
    ("한화자산운용", ["c_ebc184f1e9694bf1c26c2deb9fa552", "c_afc036ecca5cb4ded3dd37edc03a9a"], True, False),
    ("페트라자산운용", ["c_d5f00c1727f669299adba59b8f3696", "c_25bd661c5269df4dd45dda340ad0f3"], True, False),
    ("맥쿼리자산운용", ["c_cd7c50a88d23ea03c90e8a8ef8dfa3", "c_10397a3de1d41425dc5a754021939f"], True, False),
    ("이든자산운용", ["c_f1d13de4fe05811144a70d96e343a3", "c_f7b65b30aa8e7a1fe5574041aaba69"], True, False),
    ("쿼터백자산운용", ["c_56575273b455b26ca73ded3d28350e", "c_110439018b633b3b1000c73640a538"], True, False),
    ("얼라이언스번스틴자산운용", ["c_d3543ea233e5511ccae276516b4705", "c_9f7e3da7dacfc344acb19a18b25e10"], True, False),
    ("신한자산운용", ["c_c3238c9037a63208968d613e3be803", "c_efcbc5e05950de2737d2e5a52fe200"], True, False),
    ("한국투자밸류자산운용", ["c_75619b8778cd21af8b14c6bb23d73d", "c_214d507c9b765128b46227de931bb3"], True, False),
    # 생존(reg:fsc) 홈페이지가 naver.com(쓰레기) → 흡수행의 kfiam.co.kr 로 교체.
    ("한국채권투자운용", ["c_e83634393dfc00030a592029115386", "c_27ce3a4c68b0dc9082063519e0bc86"], True, True),
    # 흡수행 rdasset.co.kr = 대한인지중재치료학회(오도메인) → 연락처 이동 금지.
    ("제이엠씨자산운용", ["c_eb45474f46e8af55dabf2aea6b4e45", "c_ffabb5156ec81b03aff6ca57f562b6"], False, False),
]
_KEY_RANK = {"reg:dart": 0, "reg:fsc": 1, "reg": 2, "dom": 3, "name": 4}


def _rank(ck: str) -> int:
    for pfx in ("reg:dart", "reg:fsc"):
        if ck.startswith(pfx):
            return _KEY_RANK[pfx]
    return _KEY_RANK.get(ck.split(":", 1)[0], 9)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    settings = Settings()
    if settings.dry_run:
        raise SystemExit("DRY_RUN=true — 중단")
    sm = get_sessionmaker(settings)
    now = datetime.now(timezone.utc)
    absorbed_total = moved_contacts = adopted = 0
    with sm() as s:
        for label, ids, move_contacts, homepage_from_absorbed in GROUPS:
            rows = {c.id: c for c in s.execute(select(CompanyRow).where(CompanyRow.id.in_(ids))).scalars()}
            missing = [i for i in ids if i not in rows]
            if missing:
                print(f"[{label}] 행 없음 {missing} — 스킵")
                continue
            contacts = {i: s.execute(select(ContactRow).where(ContactRow.company_id == i)).scalars().all()
                        for i in ids}
            surv_id = sorted(ids, key=lambda i: (_rank(rows[i].canonical_key), -len(contacts[i]), i))[0]
            surv = rows[surv_id]
            srq = s.get(ReviewQueueRow, review_id_for(surv_id, "email"))
            print(f"[{label}] 생존 {surv_id} ({surv.canonical_key}, {surv.homepage}, 큐={srq.status if srq else '-'})")
            have = {(c.type, c.value.lower()) for c in contacts[surv_id]}
            cands = candidate_values_of(srq) if srq else []
            selected_default = srq.selected if srq else None
            for aid in ids:
                if aid == surv_id:
                    continue
                a = rows[aid]
                arq = s.get(ReviewQueueRow, review_id_for(aid, "email"))
                print(f"   흡수 {aid} ({a.canonical_key}, {a.homepage}, 큐={arq.status if arq else '-'}, "
                      f"연락처 {len(contacts[aid])}{'' if move_contacts else ' — 이동 안 함'})")
                if move_contacts:
                    for c in contacts[aid]:
                        if (c.type, c.value.lower()) in have:
                            continue
                        nid = contact_id_for(surv_id, c.type, c.value)
                        s.add(ContactRow(id=nid, company_id=surv_id, type=c.type, value=c.value, role=c.role,
                                         extract_method=c.extract_method, confidence=c.confidence))
                        ev = s.get(EmailValidationRow, c.id)
                        if ev is not None:
                            s.flush()
                            s.add(EmailValidationRow(contact_id=nid, status=ev.status, mx=ev.mx,
                                                     domain_match=ev.domain_match, smtp=ev.smtp,
                                                     provider=ev.provider, checked_at=ev.checked_at))
                        have.add((c.type, c.value.lower()))
                        moved_contacts += 1
                        if c.type == "email" and c.value not in cands:
                            cands.append(c.value)
                # 큐: 확정 판단 승계(생존자가 미확정일 때).
                if arq is not None and arq.status == "confirmed" and (srq is None or srq.status != "confirmed"):
                    if srq is None:
                        enqueue_email_review(s, surv_id, cands, selected_default=None)
                        srq = s.get(ReviewQueueRow, review_id_for(surv_id, "email"))
                    for col in ("status", "assignee", "assignee_id", "reviewed_at", "selected",
                                "selected_by_human", "note", "has_attachment", "manager"):
                        setattr(srq, col, getattr(arq, col))
                    if arq.selected and arq.selected not in cands:
                        cands.append(arq.selected)
                    selected_default = arq.selected
                    adopted += 1
                    print(f"      ↳ 확정 판단 승계({arq.assignee}, 선택={arq.selected})")
                if homepage_from_absorbed and a.homepage:
                    print(f"      ↳ 홈페이지 {surv.homepage} → {a.homepage}")
                    surv.homepage = a.homepage
                # 발견원장 duplicate_of(공식 표기).
                adc = s.get(DiscoveredCompanyRow, a.canonical_key)
                if adc is not None and adc.duplicate_of is None and a.canonical_key != surv.canonical_key:
                    adc.duplicate_of, adc.merged_at, adc.merged_by = surv.canonical_key, now, "manual"
                    adc.merge_reason = f"company 병합({label}) 2026-09-01"
                s.delete(a)  # contact/review_queue CASCADE
                absorbed_total += 1
            if cands or srq is not None:
                s.flush()
                keep_status = None
                if srq is not None:
                    keep_status = (srq.status, srq.assignee, srq.assignee_id, srq.reviewed_at,
                                   srq.selected_by_human, srq.claimed_by, srq.claimed_at)
                enqueue_email_review(s, surv_id, cands, selected_default=selected_default)
                srq = s.get(ReviewQueueRow, review_id_for(surv_id, "email"))
                if keep_status:  # enqueue 는 후보만 갱신하지만 selected 자동값은 바꿀 수 있어 되돌림.
                    (srq.status, srq.assignee, srq.assignee_id, srq.reviewed_at,
                     srq.selected_by_human, srq.claimed_by, srq.claimed_at) = keep_status
                    if selected_default in cands:
                        srq.selected = selected_default
            s.flush()
        print(f"\n흡수 {absorbed_total}행 · 연락처 이동 {moved_contacts} · 확정 승계 {adopted}")
        if args.apply:
            s.commit()
            print("적용 완료")
        else:
            s.rollback()
            print("미리보기 — 적용하려면 --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
