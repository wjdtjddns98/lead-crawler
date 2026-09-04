"""파이프라인 본체 — 발견부터 CompanyLead 까지.

제약 ①(중복) : ``seen``(canonical_key) + ``seen_domains``(정규화 도메인) 집합으로 이미
본 기업을 스킵 — 같은 기업이 reg:/dom: 등 다른 key 로 잡혀도 도메인 동치로 한 번만 추출.
제약 ②(실존) : ExistenceVerifier 로 죽은 기업을 거른다(검증 큐 대상).
dry_run 에서는 모든 단계가 네트워크 없이 결정적으로 동작한다.
"""

from __future__ import annotations

from typing import Any

import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..cost_ledger import CostLedger
from ..emailrules import accepted_emails, cap_emails
from ..enrich.enricher import Enricher
from ..enrich.industry_classify import SupportsClassifier, build_classifier
from ..enrich.name_eng import SupportsNameEng, build_name_eng
from ..logging import get_logger
from ..models import (
    Company,
    CompanyLead,
    Contact,
    ContactType,
    EmailValidation,
    ExtractMethod,
    Listed,
)
from ..dedup import normalize_domain
from ..dedup_resolve.inline import find_inline_duplicate
from ..dedup_resolve.inline_lexical import InlineLexicalMatcher
from ..sources.base import DiscoveredCompany, Segment, needs_english_name
from ..sources.domain_resolver import DomainResolver
from ..sources.taxonomy import AMBIGUOUS_LABELS
from ..sources.http import HostRateLimiters
from ..sources.registry import build_sources, close_sources, discover_segment
from ..storage.dart_cache import DbDartCorpCache
from ..storage.db import get_sessionmaker
from ..storage.discovery_cursor import DbCursorStore
from ..storage.nps import NpsStore
from ..storage.repository import (
    load_seen_domains,
    load_seen_keys,
    save_discovered,
    save_lead,
)
from ..verify.email_validator import EmailValidator
from ..verify.existence import ExistenceVerifier
from ..verify.registry_active import build_registry_checker

log = get_logger("pipeline")

# 진행현황 콜백 시그니처 — 카운터 dict 를 받는다(웹 직접 크롤의 실시간 표시·DB 적재용).
# 키: segments_total·segments_done·discovered(중복제외 발견)·enriched(보강완료)·saved(실존저장).
ProgressCallback = Callable[[dict[str, int]], None]

# ponytail: bounded window 상한(in-flight 세그먼트 수)을 dry_run 테스트에서 결정적으로 단정
# 하기 위한 관측 훅. 프로덕션은 None → 호출부의 None 체크 한 번뿐(no-op). 배리어(전량 선제출)
# 경로였다면 이 값이 window 를 초과했을 것이라 EP1 회귀를 잡는다. 테스트가 monkeypatch 로 설정.
_WINDOW_OBSERVER: Callable[[int], None] | None = None

# ponytail: 스테이지 병목 계측 관측 훅(테스트 단정용, 프로덕션 None→no-op). 런 종료 시 발견/추출
# worker-초 합계·straggler(최장 발견 세그먼트) 요약 dict 을 받는다. sub-segment chunking(후속
# 대형작업)이 "discovery 가 진짜 병목이고 straggler 가 있을 때만" 값어치라, 그 결정을 라이브
# 데이터로 게이팅하기 위한 계측. 요약은 항상 log.info('crawl.stage_timing') 으로도 남는다.
_TIMING_OBSERVER: Callable[[dict[str, object]], None] | None = None


def _interleave_by_country(segments: list[Segment]) -> list[int]:
    """세그먼트 인덱스를 국가 라운드로빈으로 재배열해 반환한다(병렬 제출 순서 전용).

    국가×업종 곱집합은 국가별로 뭉쳐 있어, 병렬 워커가 첫 웨이브에 같은 국가(=같은
    등록처 호스트) 세그먼트만 잡으면 호스트 레이트캡을 나눠 쓰며 서로 목을 조른다.
    KR,US,GB,… 순으로 한 개씩 돌아가며 뽑으면 동시 실행분의 호스트가 분산된다.
    결과 소비(_consume)는 원래 인덱스 순서라 dedup 결정성엔 영향 없다.
    """
    groups: dict[str, list[int]] = {}
    for i, seg in enumerate(segments):
        groups.setdefault(seg.country, []).append(i)
    order: list[int] = []
    queues = list(groups.values())  # 입력 등장 순서 보존(dict 삽입 순서).
    while queues:
        rest = []
        for q in queues:
            order.append(q.pop(0))
            if q:
                rest.append(q)
        queues = rest
    return order


def drain_completed(
    pool: ThreadPoolExecutor,
    work: Callable[[Any], Any],
    items: list[Any],
    *,
    handle: Callable[[Any], None],
    beat: Callable[[], None] | None = None,
    idle_timeout: float | None = None,
    log_stuck: Callable[[list[Any]], None] | None = None,
) -> list[Any]:
    """배치 항목을 풀에 제출하고 **완료되는 순서대로** ``handle`` 한다(``pool.map`` 의 순서 소비 대체).

    ``pool.map`` 은 제출 순서대로만 결과를 내놓아, 앞 항목 하나가 느리면(연결이 무응답으로
    사라지는 도메인·프로브 타임아웃 합산) 다른 워커가 뒤 항목을 다 끝내도 소비 루프가 멈춘 채
    ``beat`` 가 0 → 정체 워치독이 정상 진행 중인 자식을 rc=86 으로 죽이고, 재스폰이 같은
    배치·같은 항목에서 또 멈추는 결정적 루프가 된다(2026-09-01 EDINET 승격 잡 3회 연속 실패
    실사고). 완료 순 소비면 진행 신호가 실제 진행을 따라간다.

    ``idle_timeout`` 동안 **아무 항목도 완료되지 않으면** 남은 항목을 '멈춤'으로 격리해 반환한다
    (호출자가 failed 로 집계·커서 전진) — 자식을 죽이지 않고 배치를 끝내려는 것. 멈춘 워커
    스레드는 자기 프로브 타임아웃까지 살아 있다가 끝난다(풀 종료 대기는 그만큼 길어질 수 있음).
    None 이면 무한 대기(dry/테스트).
    """
    idle_timeout = idle_timeout if idle_timeout and idle_timeout > 0 else None  # 0/음수=무한(워치독 규약).
    futs = {pool.submit(work, it): it for it in items}
    pending = set(futs)
    while pending:
        done, pending = wait(pending, timeout=idle_timeout, return_when=FIRST_COMPLETED)
        if not done:
            stuck = [futs[f] for f in pending]
            for f in pending:
                f.cancel()  # 아직 시작 안 한 것은 취소, 실행 중인 것은 알아서 끝나게 둔다.
            if log_stuck is not None:
                log_stuck(stuck)
            return stuck
        for f in done:
            if beat is not None:
                beat()
            handle(f.result())
    return []


def stuck_idle_for(stall_exit_s: float | None) -> float | None:
    """drain_completed 의 idle_timeout — 정체 워치독(stall_exit_s)보다 **먼저**(0.8배) 발동해
    '멈춤 격리 → 완료분 커밋 → 커서 전진' 경로가 rc=86 kill 과의 경합에서 이기게 한다(리뷰 LOW)."""
    return stall_exit_s * 0.8 if stall_exit_s and stall_exit_s > 0 else None


def _close_in_workers(pool: ThreadPoolExecutor, close_own: Callable[[], None]) -> None:
    """풀의 **워커 스레드 자신**이 자기 스레드로컬 컴포넌트를 닫게 한다.

    Playwright sync API 는 greenlet 스레드 친화라 메인스레드에서 close() 하면
    ``greenlet.error`` 로 조용히 실패해(기존 best-effort except 가 삼킴) 브라우저·node
    드라이버가 통째로 샌다 — 배치마다 반복되며 2026-07-31 백필 5.5GB OOM 의 뿌리.
    배리어로 풀의 전 스레드가 ``close_own`` 을 정확히 한 번씩 실행하게 강제한다.
    풀이 살아있는 동안(shutdown 전) 호출해야 한다.
    """
    n = len(pool._threads)  # ponytail: private 이지만 실제 스폰된 스레드 수의 유일한 출처.
    if n == 0:
        return
    barrier = threading.Barrier(n)

    def _run() -> None:
        try:
            close_own()
        except Exception:  # 정리 실패는 무시(베스트에포트) — 배리어 참여는 보장.
            pass
        finally:
            try:
                barrier.wait(timeout=60)  # 전 스레드 분배 보장. 타임아웃=행 방지.
            except Exception:
                pass

    for f in [pool.submit(_run) for _ in range(n)]:
        try:
            f.result(timeout=90)
        except Exception:  # 개별 실패는 무시 — 남은 정리는 프로세스 종료가 회수.
            pass


def _listed_of(dc: DiscoveredCompany) -> Listed:
    """발견 단계 상장정보 문자열을 :class:`Listed` 로 안전 변환(미상 fallback)."""
    try:
        return Listed(dc.listed)
    except ValueError:
        return Listed.UNKNOWN


def _build_lead(
    dc: DiscoveredCompany,
    *,
    enricher: Enricher,
    existence: ExistenceVerifier,
    email_validator: EmailValidator,
    classifier: SupportsClassifier,
    name_eng: SupportsNameEng | None = None,
) -> CompanyLead:
    """기업 1건: 연락처 보강 + 실존 검증 + 이메일 검증 → CompanyLead.

    ``name_eng`` 가 있으면 원어 표시명(KR 제외)을 홈페이지 근거 영문 상호로 바꾼다 — ``dc`` 를
    **제자리 갱신**(name=영문·name_eng=원어)해 호출부의 ``_persist_lead`` → ``save_discovered``
    (#412 교체 규칙)·``save_lead`` 가 원장·company 양쪽에 같은 이름을 싣게 한다.

    seen·progress·leads·파이프라인 DB 세션에는 접근하지 않는다(그건 메인 스레드 전담).
    단, 라이브에서 enrich/validate 는 주입된 **cost_ledger 를 공유**해 record 하고(persist
    모드면 cost_ledger 테이블에 자체 짧은 세션으로 기록), 이 부분의 스레드안전은
    ``CostLedger`` 내부 락에 의존한다 — 워커가 '순수'해서가 아니다. 즉 워커 간 공유 가변
    상태는 cost_ledger 하나뿐이고, lead/company 테이블 적재만 메인 스레드 단독이다.
    """
    contacts = enricher.enrich(dc)
    candidates = accepted_emails(contacts)
    email = candidates[0] if candidates else None
    phone = next((c for c in contacts if c.type is ContactType.PHONE), None)
    if phone is None and dc.phone:
        # 크롤로 못 잡으면 등록처(NPS·EDGAR·FSC·DART)가 준 대표전화로 폴백 — 무비용.
        phone = Contact(
            type=ContactType.PHONE, value=dc.phone,
            extract_method=ExtractMethod.API, confidence=0.9,
        )
    form = next((c for c in contacts if c.type is ContactType.FORM), None)
    # enrich 가 이미 받은 home 생존신호를 넘겨 실존검증의 중복 HTTP 왕복을 없앤다(architect C).
    # 헤드리스로 렌더한 home 도 넘겨, verify_headless 가 같은 도메인을 또 렌더하지 않게 한다.
    ex = existence.verify(
        dc.domain,
        registry=dc.registry,
        registry_id=dc.registry_id,
        home_html=enricher.last_home_html,
        rendered_html=enricher.last_home_rendered_html,
    )
    # 구분(업종) 실질화: 등록처 코드로 대분류가 안 잡혀 미분류이거나 catch-all(모호)이면
    # 무조건 LLM 한번 거쳐 닫힌 대분류에 배치한다. 확신 라벨은 스킵(비용). abstain 이면
    # 원래값(미분류/기타) 유지 — 리드는 그대로 보존(제약②). dry_run/키없음이면 스텁이라 무과금.
    # **is_active 게이트**: 실존 확인(적재 대상) 회사만 분류한다 — 비활성(도메인 없는 GLEIF
    # 엔티티 등)은 company 테이블에 안 실리므로 분류해도 버려져 LLM 비용만 낭비된다.
    industry = dc.industry
    # **homepage 게이트**: 홈페이지 텍스트가 있어야 신뢰 분류 — 도메인·홈페이지 없는 등록처
    # 껍데기 회사(CH UK 법인 다수)를 이름만으로 LLM 블라인드 분류하면 오라벨(자동차 편중)+
    # 비용만 든다 → 홈페이지 없으면 스킵(미분류 유지). last_home_html 은 회사별 초기화됨.
    if ex.is_active and industry in AMBIGUOUS_LABELS and enricher.last_home_html:
        # 방어: 분류기는 계약상 예외를 안 던지지만(abstain), 만일 던져도 실존 리드를 잃지
        # 않도록 흡수한다(제약② — 배치 catch 로 리드가 통째 드롭되는 것 방지).
        try:
            verdict = classifier.classify(dc.name, dc.domain, enricher.last_home_html)
            if verdict.label:
                industry = verdict.label
        except Exception as exc:
            log.info("pipeline.classify.error", key=dc.canonical_key, err=str(exc))
    # 표시명 영문 우선(KR 제외, PO 2026-09-04): 소스가 영문명을 못 준 원어 표시명은 홈페이지
    # 본문에 실제 적힌 영문 상호로만 교체(추측 금지·abstain=원어 유지). 게이트는 업종 분류와
    # 동일(is_active·홈페이지 본문) — 같은 본문을 재사용하므로 추가 fetch 0.
    if (
        name_eng is not None
        and ex.is_active
        and enricher.last_home_html
        and needs_english_name(dc.name, dc.country)
    ):
        try:
            eng = name_eng.extract(dc.name, dc.domain, enricher.last_home_html)
        except Exception as exc:  # 방어 — 추출기는 abstain 계약이지만 리드 유실은 막는다.
            log.info("pipeline.name_eng.error", key=dc.canonical_key, err=str(exc))
            eng = None
        if eng:
            dc.name, dc.name_eng = eng, dc.name_eng or dc.name
    company = Company(
        canonical_key=dc.canonical_key,
        name=dc.name,
        country=dc.country,
        industry=industry,
        listed=_listed_of(dc),
        homepage=f"https://{dc.domain}" if dc.domain else None,
        domain=dc.domain,
        segment=dc.segment,
        is_active=ex.is_active,
        existence_confidence=ex.confidence,
        site_alive=ex.site_alive,
    )
    # 후보별 검증(MX/도메인/SMTP·딜리버러빌리티) — 선택 UI 에 신호 제공. validate_all_candidates
    # 가 꺼지면 선택 이메일(candidates[0])만 심층검증(SMTP/유료)하고 나머지는 형식/MX 까지만 —
    # 후보 수만큼 곱해지던 SMTP 핸드셰이크·유료 호출을 줄인다(산출의 선택 이메일 신호는 동일).
    deep_all = email_validator.settings.validate_all_candidates
    validations = {
        c.value: email_validator.validate(
            c.value, dc.domain, deep=deep_all or (email is not None and c.value == email.value)
        )
        for c in candidates
    }
    # 검증등급 우선 재정렬 + 상한(IR정상 > 그외정상 > 주의, 무효 제외, 최대 MAX_EMAILS).
    # 등급은 위 validate 결과라 여기서 처음 확정된다 → role 정렬(accepted_emails) 뒤에 온다.
    provisional = email
    candidates = cap_emails(candidates, {v: ev.status for v, ev in validations.items()})
    email = candidates[0] if candidates else None
    # 상한 밖 후보의 검증결과는 버린다(저장 대상이 아니므로 email_validations 도 동기화).
    validations = {c.value: validations[c.value] for c in candidates}
    # 재정렬로 선택 이메일이 바뀌었는데 얕게만 검증됐다면, 최종 선택분은 심층검증한다
    # (선택 이메일은 SMTP/딜리버러빌리티까지 본다는 기존 계약 유지).
    if (
        email is not None
        and not deep_all
        and (provisional is None or email.value != provisional.value)
    ):
        validations[email.value] = email_validator.validate(email.value, dc.domain, deep=True)
        # 심층검증은 등급을 **떨어뜨릴 수 있다**(SMTP 미배달·딜리버러빌리티 → INVALID).
        # 그대로 두면 방금 '무효 제외'로 거른 계약이 깨져 INVALID 이메일이 선택으로 남는다
        # → 상한·정렬을 한 번 더 적용한다. 재적용은 1회뿐이라 루프가 아니며, 새 선두가
        # 얕은 등급이면 그대로 둔다(선택 안 된 후보와 같은 취급 — 기존 동작).
        candidates = cap_emails(candidates, {v: ev.status for v, ev in validations.items()})
        email = candidates[0] if candidates else None
        validations = {c.value: validations[c.value] for c in candidates}
    validation = (
        validations.get(email.value, EmailValidation()) if email else EmailValidation()
    )
    return CompanyLead(
        company=company,
        email=email,
        email_candidates=candidates,
        phone=phone,
        form=form,
        email_validation=validation,
        email_validations=validations,
    )


def run_pipeline(
    segments: Iterable[Segment],
    *,
    seen: set[str] | None = None,
    settings: Settings | None = None,
    persist: bool = False,
    record_only: bool = False,
    on_progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
    target_saved: int | None = None,
) -> list[CompanyLead]:
    """세그먼트들을 처리해 검증된 :class:`CompanyLead` 목록을 반환한다.

    ``persist=True`` 면 DB 세션을 열어 ① 발견 원장(discovered_company)에 모든 신규
    기업을 기록(죽은 기업도 — 제약 ① 재추출 방지)하고, ② 실존(active) 기업만 회사·
    연락처 테이블에 저장한다(제약 ②). 기존 ``seen`` 은 원장 key 와 합쳐 dedup 시드가 된다.

    ``record_only=True`` 면 dedup·inline 도메인 해석까지는 그대로 하되 ②를 건너뛴다 —
    신규 기업을 추출 큐(``pending``)에 넣지 않고 원장에만 기록한다(``_persist_touch``).
    트랙 S(세그먼트 작업 큐, ``docs/segment-jobs-design.md`` §3)의 발견 단계 전용 — 승격
    (추출)은 별도 ``pipeline/promote.py`` 가 원장을 다시 훑어 담당한다. ``persist=False``
    와 조합하면(세션 없음) 아무것도 기록되지 않고 dedup 만 수행된다(호출자 계약 밖).

    ``on_progress`` 가 주어지면 발견/보강/저장·세그먼트 진행 카운터 dict 를 단계마다
    호출한다(웹 직접 크롤의 실시간 현황). ``should_cancel`` 이 매 기업 처리 직전 True 를
    반환하면 협조적으로 중단한다(이미 처리된 결과는 보존). 둘 다 None 이면 기존 동작 그대로.

    ``target_saved`` 가 주어지면 실존 저장(``saved``) 누계가 그 값에 도달하는 즉시 세그먼트를
    더 돌지 않고 조기 종료한다("정해진 양만큼 뽑고 멈춤"). None(기본)이면 주어진 세그먼트를
    전부 깊게 소진한다(소스당 ``discovery_max_per_source`` 까지). 어느 경우든 dedup(제약①)으로
    이미 본 기업은 건너뛰므로, 같은 스코프 재크롤이 아니라 **새 기업을 계속 발견**할 때 채워진다.

    조기종료는 배치(flush) 경계에서만 평가하므로 정확히 ``target_saved`` 에서 멈추지 않고
    최대 ``batch_size``(병렬 시 ``workers*4``, 순차 시 1)만큼 오버슈트할 수 있다 — 마지막
    배치가 통째로 처리·적재된 뒤 카운터를 확인하기 때문. 상한이 아니라 하한 보장이다
    (``saved >= target_saved``).
    """
    settings = settings or get_settings()
    seg_list = list(segments)
    progress = {
        "segments_total": len(seg_list),
        "segments_done": 0,
        "discovered": 0,
        "enriched": 0,
        "saved": 0,
    }

    def _emit() -> None:
        if on_progress is not None:
            on_progress(dict(progress))

    def _target_hit() -> bool:
        """목표 실존 저장수(target_saved)에 도달했는지 — 도달 시 세그먼트 순회를 조기 종료."""
        return target_saved is not None and progress["saved"] >= target_saved
    seen = seen if seen is not None else set()
    # 도메인 동치 dedup(제약 ①) — 같은 기업이 등록처 key(reg:)와 검색 key(dom:)로 다르게
    # 잡혀도 정규화 도메인이 같으면 한 번만 추출한다. seen(키)과 짝을 이뤄 런 전체·DB 영속을
    # 가로질러 적용된다(within-segment 머지는 discover_segment 가 1차로 수행).
    seen_domains: set[str] = set()
    # 런당 단일 공유 호스트 레이트리미터 — 발견(등록처 스캔)과 검증(registry_active 의 CH
    # `/company` 조회)이 **같은 호스트·같은 API 키**를 쓰므로 합산 발사율을 한 레지스트리로
    # 묶는다. 따로 만들면 각자 캡 안이어도 합산이 CH 키 쿼터(600/5분=2req/s)를 초과해 429
    # (2026-07-10 실사고). dry_run 은 네트워크 없음이라 무해(주입만 되고 미사용).
    host_limiters = HostRateLimiters(default_rate=settings.discovery_rate_per_host)
    # 라이브에서만 등록처 active 체커 주입(키 있을 때) — 실존 판정의 최강 신호(active=0.9 우선).
    # dry_run 은 도메인 유무로 결정적이라 미주입.
    registry_checker = (
        None
        if settings.dry_run
        else build_registry_checker(settings, rate_limiters=host_limiters)
    )
    existence = ExistenceVerifier(settings, registry_checker=registry_checker)
    # 라이브에서만 과금 원장을 켠다(dry_run 은 유료 호출이 없음). persist 면 DB 에 누계
    # 적재(월·다중런 합산), 아니면 인메모리(현재 런 내 가드만). 예산 초과 시 유료 차단.
    cost_ledger = CostLedger(settings, persist=persist) if not settings.dry_run else None
    email_validator = EmailValidator(settings, cost_ledger=cost_ledger)
    enricher = Enricher(settings, cost_ledger=cost_ledger)
    # 산업 분류기 — 상태없는(호출당 독립 client) 공유 인스턴스라 워커 간 공유 안전(내부 락으로
    # 런당 호출 카운터만 보호). dry_run/플래그off/키없음이면 무네트워크 결정적 스텁으로 폴백.
    classifier = build_classifier(settings, ledger=cost_ledger)
    name_eng = build_name_eng(settings, ledger=cost_ledger)  # 분류기와 같은 공유·캡 규약.
    # 도메인 해석(opt-in·라이브) — 발견이 도메인을 못 준 기업(GLEIF 등)을 회사명으로 보강.
    # 없으면 enrich 가 즉시 빈손이라 사이트·이메일을 못 얻는다(핵심 커버리지 갭 해소).
    resolver = (
        DomainResolver(settings, cost_ledger=cost_ledger)
        if settings.resolve_domains and not settings.dry_run
        else None
    )

    # 기업 단위 병렬 추출 — enrich/verify/validate 는 I/O 바운드라 동시 처리로 처리량을 올린다.
    # 워커>1 이면 ThreadPool + 워커별 독립 인스턴스(공유 throttle 경쟁 회피). dedup·카운터·DB
    # 적재는 메인 스레드 전담. pool.map 순서보존 + _build_lead 결정성 → workers 무관 산출 동일.
    workers = settings.enrich_workers
    pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    _tl = threading.local()
    _created: list[object] = []  # 워커별 생성 인스턴스(종료 시 close).
    _created_lock = threading.Lock()
    # 스테이지 병목 계측(관측용, 정확성 무관). 발견 = 세그먼트별 (라벨, 초, 회사수), 추출 =
    # _build_lead 별 초. list.append 는 GIL-원자라 병렬 워커에서도 안전(락 불필요).
    disco_durations: list[tuple[str, float, int]] = []
    enrich_durations: list[float] = []

    def _process_one(dc: DiscoveredCompany) -> CompanyLead:
        if pool is None:  # 순차 — 공유 인스턴스(메인 스레드 단독 실행, 기존 경로와 동일).
            t0 = time.monotonic()
            lead = _build_lead(
                dc,
                enricher=enricher,
                existence=existence,
                email_validator=email_validator,
                classifier=classifier,
                name_eng=name_eng,
            )
            enrich_durations.append(time.monotonic() - t0)
            return lead
        w = getattr(_tl, "trio", None)
        if w is None:  # 워커 스레드당 독립 인스턴스 1회 생성. registry_checker 까지 워커별로
            # 따로 만들어 공유 Fetcher 의 throttle(self._last) 경쟁을 없앤다(아키텍트 MAJOR).
            rc = (
                None
                if settings.dry_run
                else build_registry_checker(settings, rate_limiters=host_limiters)
            )
            enr = Enricher(settings, cost_ledger=cost_ledger)
            exi = ExistenceVerifier(settings, registry_checker=rc)
            val = EmailValidator(settings, cost_ledger=cost_ledger)
            w = (enr, exi, val)
            _tl.trio = w
            with _created_lock:
                _created.extend([enr, exi, val, *([rc] if rc is not None else [])])
        enr, exi, val = w
        t0 = time.monotonic()
        lead = _build_lead(
            dc, enricher=enr, existence=exi, email_validator=val, classifier=classifier,
            name_eng=name_eng,
        )
        enrich_durations.append(time.monotonic() - t0)
        return lead

    leads: list[CompanyLead] = []
    pending: list[DiscoveredCompany] = []  # 배치 — 메인 dedup 통과분, 풀로 동시 처리.
    # workers==1(pool None)이면 배치 1 — 기업별 즉시 처리·적재로 기존 순차 동작과 동일
    # (진행카운터 타이밍·실패 시 직전까지 보존). 병렬이면 workers*4 로 모은다.
    batch_size = max(1, workers * 4) if pool is not None else 1

    def _flush() -> None:
        """대기 배치를 (병렬이면 풀로) 처리하고 결과를 메인 스레드에서 건별 적재한다.

        한 기업이 _build_lead 에서 예외가 나도 그 기업만 건너뛰고(로그) 나머지·이미 성공한
        기업은 보존한다(배치 전체 유실 방지). 입력 순서로 결과를 받아 적재하므로 leads 순서가
        발견 순서와 같다(workers 무관·순서 결정적; 라이브 내용은 네트워크 의존이라 순차와 동일
        수준으로 비결정). 적재·progress·_emit·DB 세션은 메인 스레드 전담 — 워커가 공유하는
        가변상태는 cost_ledger(자체 락) 뿐이다.
        """
        if not pending:
            return
        futures = [pool.submit(_process_one, d) for d in pending] if pool is not None else None
        for i, d in enumerate(pending):
            try:
                lead = futures[i].result() if futures is not None else _process_one(d)
            except Exception as exc:  # 기업 1건 실패 → 스킵(배치 보존). graceful 아닌 예외만 도달.
                log.warning("pipeline.process.error", key=d.canonical_key, err=str(exc))
                continue
            progress["enriched"] += 1
            leads.append(lead)
            if lead.company.is_active:
                progress["saved"] += 1  # 실존 확인분(persist 면 회사·연락처 저장됨).
            if session is not None:
                _persist_lead(session, d, lead)
            _emit()  # 기업 1건 처리 완료 — 카운터 갱신 통지(폴링 표시).
        pending.clear()

    session: Session | None = get_sessionmaker(settings)() if persist else None
    # 등록처 발견 커서(런 간 offset 영속, 딥백필) — persist 런에서만. 호출마다 자체 세션을
    # 여는 어댑터라 병렬 발견 워커에서도 안전하다. dry_run 은 _live 미진입이라 무접촉.
    cursor_store = DbCursorStore(get_sessionmaker(settings)) if persist else None
    # DART corp 캐시(persist 런) — company.json 을 corp 당 평생 1회만 조회(업종 세그먼트
    # 수 배 중복조회 제거). 호출마다 자체 세션이라 병렬 청크 워커에서도 읽기 안전.
    dart_corp_cache = DbDartCorpCache(get_sessionmaker(settings)) if persist else None
    # 국민연금 스냅샷(persist 런) — nps-import 적재분을 업종·규모 우선으로 발견 소비.
    nps_store = NpsStore(get_sessionmaker(settings)) if persist else None
    cancelled = False
    disco_sources: list = []  # finally 가 항상 참조할 수 있게 try 전 바인딩(빌드 실패 시 no-op).
    try:
        # 순차 발견(discovery_workers<=1): 발견 소스를 런 시작에 1회만 빌드해 모든 세그먼트에
        # 재사용한다(세그먼트마다 재생성·httpx 누수 제거 + keep-alive 연결 재사용). 세그먼트 간
        # 발견 루프는 단일 스레드라 공유 안전(세그먼트 내부 소스병렬(discovery_source_workers)은
        # 소스 인스턴스당 스레드 1개만 쓰므로 여전히 안전). 병렬(>1)이면 워커별 독립 sources 를
        # _discover_one 에서 따로 빌드하므로 여기선 빌드하지 않는다(공유 Fetcher throttle 경쟁 회피).
        if settings.discovery_workers <= 1:
            # 소스병렬(discovery_source_workers>1)이면 세그먼트 순차라도 소스들이 동시에
            # 나가므로, 공유 호스트 레이트리미터를 주입해 합산 발사율을 억제한다(세그먼트
            # 병렬 분기와 동일 배선). 소스병렬도 꺼져 있으면 None(기존 동작, 회귀 0).
            seq_limiters = host_limiters if settings.discovery_source_workers > 1 else None
            disco_sources = build_sources(
                settings,
                cost_ledger,
                rate_limiters=seq_limiters,
                cursor_store=cursor_store,
                dart_corp_cache=dart_corp_cache,
                nps_store=nps_store,
            )
        if session is not None:
            seen |= load_seen_keys(session)
            seen_domains |= load_seen_domains(session)
        # 인라인 렉시컬 후보 탐지(opt-in, 갭1) — 도메인 없는 신규 기업을 기존 name: 티어와
        # 이름 유사도로 대조해 dedup_candidate(워크벤치)로 적재. 자동 스킵 안 함(제약②).
        lexical_matcher = (
            InlineLexicalMatcher(session)
            if session is not None and settings.dedup_inline_lexical
            else None
        )
        _emit()  # 초기 상태(세그먼트 총수) 통지 — 시작 즉시 진행바가 보이도록.

        def _consume(discovered: Iterable[DiscoveredCompany]) -> None:
            """발견된 후보들을 **단일 스레드**에서 dedup→배치→_flush(enrich pool)→persist 한다.

            seen/seen_domains 변형·DB 세션·진행카운터는 전부 이 메인 스레드가 전담하므로
            발견 동시성(discovery_workers)과 무관하게 정확성(제약①②)이 보존된다. 세그먼트
            경계에서 _flush 로 큐를 비운다(취소 시에도 이미 발견·큐된 분은 보존).
            """
            nonlocal cancelled
            for dc in discovered:
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break  # 다음 기업 처리 전 협조적 중단(처리분은 보존).
                dom = normalize_domain(dc.domain) if dc.domain else None
                if dc.canonical_key in seen or (dom is not None and dom in seen_domains):
                    log.info("dedup.skip", key=dc.canonical_key)
                    if session is not None:
                        # C5 inline 승격(opt-in): **다른 key·같은 도메인**(교차key 중복)이면 단순
                        # touch 대신 생존자에 duplicate_of 링크 — 원장 골든레코드 그래프를 적재
                        # 시점에 완성한다(기존 동작은 cross-key 중복을 미연결 행으로 남김). auto
                        # 티어(이름高+도메인root 일치)만 링크, 그 외·같은key 는 기존대로 touch
                        # (제약② 보수 — 경계는 배치/워크벤치 위임). off 면 항상 touch(회귀 0).
                        linked = False
                        if settings.dedup_inline and dc.canonical_key not in seen and dom is not None:
                            survivor = find_inline_duplicate(session, dc)
                            if survivor is not None:
                                _persist_inline_dup(session, dc, survivor)
                                linked = True
                        if not linked:
                            # 재발견: 추출은 건너뛰되 last_crawled_at 만 갱신(재크롤 추적).
                            _persist_touch(session, dc)
                    continue
                seen.add(dc.canonical_key)
                if dom is not None:
                    seen_domains.add(dom)
                elif resolver is not None:
                    # 도메인 미보유 기업 → 회사명으로 공식 도메인 해석 시도.
                    resolved = resolver.resolve(dc)
                    rdom = normalize_domain(resolved) if resolved else None
                    if rdom is not None:
                        if rdom in seen_domains:
                            # 해석된 도메인이 이미 본 기업과 동치 → 재추출 스킵(제약 ①).
                            # 원장엔 해석된 도메인을 기록해 다음 런이 재해석(quota 낭비) 안 하게.
                            log.info("dedup.skip.resolved", key=dc.canonical_key, domain=rdom)
                            if session is not None:
                                _persist_touch(session, dc.model_copy(update={"domain": resolved}))
                            continue
                        seen_domains.add(rdom)
                        dc = dc.model_copy(update={"domain": resolved})  # 도메인만 채움

                progress["discovered"] += 1  # 중복제외 신규 발견(처리 대상 확정).
                # 도메인 없는(name: 티어) 신규 기업만 렉시컬 후보 대조(워크벤치 적재, 추출은 진행).
                if lexical_matcher is not None and dc.canonical_key.startswith("name:"):
                    lexical_matcher.consider(session, dc.canonical_key, dc.name, dc.country or "")
                if record_only:
                    # 트랙 S 발견 단계: 원장에만 기록 — 추출(pending)은 promote 단계(별도
                    # 원장 재조회)가 맡는다. _flush 는 pending 이 비어 있어 사실상 no-op.
                    if session is not None:
                        _persist_touch(session, dc)
                    _emit()
                    continue
                pending.append(dc)  # 배치 적재 — _flush 에서 (병렬이면 풀로) 동시 처리.
                if len(pending) >= batch_size:
                    _flush()
                    if _target_hit():
                        break  # 목표 실존수 도달 — 이 세그먼트 내 추가 발견 중단.
            _flush()  # 세그먼트 경계 — 큐된 분 처리(취소 시에도 이미 발견한 분은 보존).

        if settings.discovery_workers <= 1:
            # 순차 발견(기존 동작·회귀 0) — 세그먼트를 하나씩 발견하며 곧바로 처리·적재한다.
            for segment in seg_list:
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                _t0 = time.monotonic()
                found = discover_segment(
                    segment,
                    settings,
                    cost_ledger=cost_ledger,
                    sources=disco_sources,
                    seen_domains=seen_domains,  # 글로벌 dedup 시드 주입(제약①·②).
                )
                disco_durations.append((segment.label, time.monotonic() - _t0, len(found)))
                _consume(found)
                if cancelled or _target_hit():
                    break  # 취소 또는 목표 도달 → 남은 세그먼트는 돌지 않고 종료.
                progress["segments_done"] += 1
                _emit()
        elif not (should_cancel is not None and should_cancel()):
            # 병렬 발견(opt-in): 세그먼트 발견(네트워크 수집)만 동시화하고 dedup/적재는 위
            # _consume(단일 스레드)이 전담한다 — 공유 가변상태는 cost_ledger(내부 락) +
            # rate_limiters(내부 락)뿐이라 정확성(제약①② dedup·DB)이 보존된다. 워커는 각자
            # (스레드별) 독립 sources(공유 호스트 레이트리미터)로 순수 발견만 한다.
            #
            # 스트리밍(bounded window): 세그먼트를 전량 선제출하지 않고 in-flight(제출−소비)
            # 수를 window(=discovery_workers*2) 이하로 유지하며, 완료되는 대로 즉시 _consume
            # 로 흘려 발견·추출을 오버랩시킨다 — 옛 배리어(전량 선발견→메모리 스파이크·워커
            # 유휴)를 없앤다. **_consume 순서 = 완료순서**(옛 "입력순서=순차=결정적" 불변식
            # 폐기). 제약①(이중추출 금지)은 dedup 이 단일스레드 _consume 안이라 소비순서와
            # 무관하게 보존된다(교차세그먼트 중복회사의 업종라벨 귀속·leads 순서만
            # race-dependent — 기능 무해: 모호라벨은 분류기 재산정, 중복 loser 는 touch 만).
            #
            # 한계(변경 없음): ① seen_domains 는 발견 시작 스냅샷(읽기전용)이라 뒤 세그먼트가
            # 앞 세그먼트의 발견 도메인을 유료 검색 비용가드에 반영하지 못한다(중복은 _consume
            # dedup 이 걸러 정확성엔 무해, 검색비만 약간 손해 가능). ② target_saved 도달 시 남은
            # 세그먼트를 더 제출하지 않는다(유료검색 절감). ③ 취소는 미제출 세그먼트만 즉시
            # 멈춘다 — in-flight 발견은 완료까지 가며, 이미 완료·소비분은 보존된다. dry_run 은
            # 발견이 seen 과 무관·결정적이라 순차와 산출 집합이 완전히 동일.
            shared_limiters = host_limiters  # 런 공유 레지스트리(발견+검증 합산 캡).
            seen_domains_snapshot = set(seen_domains)  # 읽기전용 — 워커는 변형하지 않는다.
            # 워커 스레드별 독립 sources — 워커당 1회만 빌드해 그 워커의 모든 세그먼트에 재사용
            # (keep-alive 보존, 세그먼트마다 재빌드하던 httpx churn 제거), 풀 종료 후 일괄 close.
            disco_tl = threading.local()
            disco_created: list[list] = []
            disco_created_lock = threading.Lock()

            def _discover_one(segment: Segment) -> list[DiscoveredCompany]:
                ws = getattr(disco_tl, "sources", None)
                if ws is None:
                    ws = build_sources(
                        settings,
                        cost_ledger,
                        rate_limiters=shared_limiters,
                        cursor_store=cursor_store,
                        dart_corp_cache=dart_corp_cache,
                        nps_store=nps_store,
                    )
                    disco_tl.sources = ws
                    with disco_created_lock:
                        disco_created.append(ws)
                try:
                    _t0 = time.monotonic()
                    found = list(
                        discover_segment(
                            segment,
                            settings,
                            cost_ledger=cost_ledger,
                            sources=ws,
                            seen_domains=seen_domains_snapshot,
                        )
                    )
                    disco_durations.append(
                        (segment.label, time.monotonic() - _t0, len(found))
                    )
                    return found
                except Exception as exc:  # 한 세그먼트 발견 실패 → 그 세그먼트만 빈 결과로 격리
                    # (전체 런 중단·기적재 유실 방지 — 순차의 건별 보존과 동등한 blast-radius).
                    log.warning("pipeline.discover.error", segment=segment.label, err=str(exc))
                    return []

            # bounded submission window: interleave 순서로 window 개를 초기 제출하고, 완료된
            # 세그먼트를 _consume 한 뒤에야 다음을 제출(슬라이딩)해 in-flight ≤ window 를
            # 보장한다(미소비 발견결과 무한누적 방지). refill 도 interleave 순서라 같은국가
            # 호스트 재군집이 없다. target 도달·취소면 다음 제출을 멈춘다.
            window = max(1, settings.discovery_workers * 2)  # 풀 + 버퍼(=풀). 작으면 발견풀
            # starve, 크면 메모리바운드 약화 — in-flight(제출−소비) 세그먼트 수의 상한.
            order = iter(_interleave_by_country(seg_list))
            try:
                with ThreadPoolExecutor(max_workers=settings.discovery_workers) as disco_pool:
                    inflight: dict = {}

                    def _submit_next() -> None:
                        idx = next(order, None)
                        if idx is not None:
                            inflight[disco_pool.submit(_discover_one, seg_list[idx])] = idx

                    for _ in range(window):
                        _submit_next()  # 윈도 초기 충전.
                    if _WINDOW_OBSERVER is not None:
                        _WINDOW_OBSERVER(len(inflight))
                    while inflight and not cancelled and not _target_hit():
                        done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                        for fut in done:
                            inflight.pop(fut)
                            if should_cancel is not None and should_cancel():
                                cancelled = True  # 미제출 세그먼트 제출 중단(in-flight 는 완료까지).
                                break
                            _consume(fut.result())  # _discover_one 예외격리 → 항상 list.
                            if cancelled or _target_hit():
                                break  # 취소(consume 내부)·목표도달 → 추가 제출 중단.
                            progress["segments_done"] += 1
                            _emit()
                            _submit_next()  # 소비한 만큼만 refill → in-flight ≤ window.
                            if _WINDOW_OBSERVER is not None:
                                _WINDOW_OBSERVER(len(inflight))
            finally:
                for ws in disco_created:
                    close_sources(ws)  # 워커별 sources httpx 정리(누수 방지).
    finally:
        if pool is not None:
            # Playwright 보유 컴포넌트(enr/exi)는 만든 워커 스레드만 닫을 수 있다 —
            # 메인스레드 close 는 greenlet.error 로 no-op(누수). shutdown 전에 워커가 직접.
            def _close_own_trio() -> None:
                trio = getattr(_tl, "trio", None)
                if trio is not None:
                    for obj in trio:
                        close = getattr(obj, "close", None)
                        if callable(close):
                            close()

            _close_in_workers(pool, _close_own_trio)
            pool.shutdown(wait=True)  # 진행 중 워커 완료 대기 후 인스턴스 정리(쓰기 경쟁 없음).
        # 나머지(rc httpx 등) + 메인스레드(순차 경로) 인스턴스 정리 — 워커가 이미 닫은 것은
        # close() 가 멱등(내부 필드 None 가드)이라 재호출해도 안전.
        for obj in (*_created, enricher, existence, email_validator):
            close = getattr(obj, "close", None)
            if callable(close):
                close()
        close_sources(disco_sources)  # 발견 소스 httpx 클라이언트 정리(런당 1회).
        if session is not None:
            session.close()
    # 스테이지 병목 요약(관측용) — 발견 vs 추출 worker-초 + straggler. chunking 후속의 게이트
    # 데이터: enrich 가 지배적이면 chunking(발견 병렬화)은 헛일, discovery 가 크고 한 세그먼트에
    # 몰려 있으면(share 高) 값어치. dry_run 은 시간이 미미해 상대 비교만 의미.
    if disco_durations or enrich_durations:
        disco_sec = sum(d for _, d, _ in disco_durations)
        enrich_sec = sum(enrich_durations)
        straggler = max(disco_durations, key=lambda x: x[1], default=("", 0.0, 0))
        summary: dict[str, object] = {
            "discovery_worker_sec": round(disco_sec, 2),
            "enrich_worker_sec": round(enrich_sec, 2),
            "segments": len(disco_durations),
            "companies": sum(n for _, _, n in disco_durations),
            "straggler_seg": straggler[0],
            "straggler_sec": round(straggler[1], 2),
            "straggler_share": round(straggler[1] / disco_sec, 2) if disco_sec else 0.0,
            "max_seg_companies": max((n for _, _, n in disco_durations), default=0),
            "bottleneck": "enrich" if enrich_sec > disco_sec else "discovery",
        }
        log.info("crawl.stage_timing", **summary)
        if _TIMING_OBSERVER is not None:
            _TIMING_OBSERVER(summary)
    return leads


def _persist_touch(session: Session, dc: DiscoveredCompany) -> None:
    """재발견 기업의 last_crawled_at 만 갱신(per-company 트랜잭션)."""
    try:
        save_discovered(session, dc)
        session.commit()
    except IntegrityError:
        session.rollback()


def _persist_inline_dup(session: Session, dc: DiscoveredCompany, survivor_key: str) -> None:
    """inline auto-중복으로 판정된 신규 리드를 원장에 기록하고 기존 생존자에 흡수한다(가역).

    원장엔 항상 남기되(제약① 재추출 방지) ``duplicate_of`` + 머지 audit 를 적어 가역
    추적한다. 회사 본체·연락처는 만들지 않는다(추출 스킵). per-company 트랜잭션.
    """
    from datetime import datetime, timezone

    from ..schema import DiscoveredCompanyRow

    try:
        save_discovered(session, dc)
        row = session.get(DiscoveredCompanyRow, dc.canonical_key)
        linked = (
            row is not None and row.duplicate_of is None and dc.canonical_key != survivor_key
        )
        if linked:
            row.duplicate_of = survivor_key
            row.merged_at = datetime.now(timezone.utc)
            row.merged_by = "auto"
            # merge_reason 은 stable 토큰(report/rollback 파싱용) — schema.py 컨벤션 준수.
            row.merge_reason = "inline:name+domain"
        session.commit()
        # 실제 링크가 써졌을 때만 absorb 로그(이미 링크됨/가드 실패 시 오탐 audit 방지).
        if linked:
            log.info("dedup.inline.absorb", key=dc.canonical_key, survivor=survivor_key)
        else:
            log.info("dedup.inline.touch", key=dc.canonical_key)
    except IntegrityError:
        session.rollback()
        log.info("persist.skip.conflict", key=dc.canonical_key)


def _persist_lead(session: Session, dc: DiscoveredCompany, lead: CompanyLead) -> bool:
    """한 기업을 독립 트랜잭션으로 영속화한다.

    원장은 항상 기록(제약 ①), 회사 본체는 실존(active)만 저장(제약 ②). 동시 워커가
    같은 기업을 먼저 적재해 PK/UNIQUE 충돌이 나면 해당 기업만 스킵(배치 전체 보호).
    그 외 DB 예외도 기업 1건 격리로 흡수한다 — 비정상 데이터 1건(예: 컬럼 길이 초과)이
    연속(24/7) 크롤 잡 전체를 죽인 실사고의 방어선. rollback 으로 세션을 살려 다음
    기업 적재를 계속한다. 반환 = 커밋 성공 여부 — 호출자는 이 값으로만 promoted/emails 를
    센다(저장 실패를 성공으로 집계하던 카운터 부풀림 차단, 세그먼트 잡 대시보드 신뢰도).
    """
    try:
        save_discovered(session, dc)
        if lead.company.is_active:
            save_lead(session, lead, source=dc.source)
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        log.info("persist.skip.conflict", key=dc.canonical_key)
        return False
    except Exception as exc:
        session.rollback()
        log.warning("persist.skip.error", key=dc.canonical_key, err=str(exc)[:300])
        return False
