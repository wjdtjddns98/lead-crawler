"""국민연금 가입 사업장 스냅샷 저장소 — 월간 CSV 인제스트 + 발견용 조회.

원천: 공공데이터포털 "국민연금공단_국민연금 가입 사업장 내역" 월간 파일(가입자 3인+
법인 중심, ~수십만 행). CLI ``nps-import`` 가 :func:`ingest_nps_csv` 로 통째 적재하고
(스냅샷 교체 — 이전 적재분 삭제), :class:`NpsStore` 가 발견 소스(NpsSource)에
업종 접두 매칭 + 가입자수 내림차순(대형 우선) 페이지를 준다.

CSV 는 헤더명 매칭으로 파싱한다(컬럼 순서 무의존). 인코딩은 utf-8-sig → cp949 순으로
시도(공공데이터 파일 관행). 파싱 실패 행은 건너뛰고 카운트만 남긴다(전량 적재 우선).
"""

from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from ..logging import get_logger
from ..schema import KsicIndustryMapRow, NpsWorkplaceRow

log = get_logger("storage.nps")

_BATCH = 2000  # 인제스트 커밋 배치 — 수십만 행 단일 트랜잭션 방지.

# CSV 헤더명 → 내부 필드. 원천 헤더의 사소한 표기 변형(공백 등)은 strip 후 비교.
_COL_NAME = "사업장명"
_COL_BIZNO = "사업자등록번호"
_COL_ADDR_ROAD = "사업장도로명상세주소"
_COL_ADDR_JIBUN = "사업장지번상세주소"
_COL_IND_CODE = "사업장업종코드"
_COL_IND_NAME = "사업장업종코드명"
_COL_SUBS = "가입자수"
_COL_AMT = "당월고지금액"
_COL_STATUS = "사업장가입상태코드"
_COL_RESIGNED = "탈퇴일자"
_COL_YM = "자료생성년월"


def _to_int(value: str | None) -> int:
    try:
        return int(str(value or "").replace(",", "").strip() or 0)
    except ValueError:
        return 0


def _open_csv(path: Path):
    """utf-8-sig → cp949 순으로 열어 DictReader 를 반환한다(공공데이터 인코딩 관행)."""
    for enc in ("utf-8-sig", "cp949"):
        f = None
        try:
            f = path.open(encoding=enc, newline="")
            reader = csv.DictReader(f)
            # 헤더에 핵심 컬럼이 있어야 올바른 인코딩으로 판정(cp949 를 utf-8 로 잘못
            # 읽으면 헤더가 깨져 여기서 걸러진다).
            fields = [(name or "").strip() for name in (reader.fieldnames or [])]
            if _COL_NAME in fields:
                return f, reader
            f.close()
        except (UnicodeDecodeError, OSError):
            if f is not None:  # fieldnames 읽기 중 디코딩 실패 — 핸들 정리 후 다음 인코딩.
                f.close()
            continue
    raise ValueError(f"CSV 헤더에서 '{_COL_NAME}' 컬럼을 찾지 못함(인코딩/서식 확인): {path}")


def _row_to_model(raw: dict) -> NpsWorkplaceRow | None:
    """원천 행(dict — CSV/DictReader·odcloud API JSON 공용, 한국어 헤더 키)을 모델로.

    사업장명 없는 행은 None(건너뜀). 값은 전부 방어적 문자열화·절단(신뢰불가 공공 파일).
    """
    row = {str(k or "").strip(): str(v if v is not None else "").strip() for k, v in raw.items()}
    name = row.get(_COL_NAME, "")
    if not name:
        return None
    bizno = row.get(_COL_BIZNO, "").replace("-", "")[:6] or None
    addr = row.get(_COL_ADDR_ROAD) or row.get(_COL_ADDR_JIBUN) or None
    return NpsWorkplaceRow(
        data_ym=row.get(_COL_YM, "")[:6],
        name=name[:512],
        bizno_prefix=bizno,
        address=(addr or "")[:512] or None,
        industry_code=(row.get(_COL_IND_CODE, "") or "")[:8] or None,
        industry_name=(row.get(_COL_IND_NAME, "") or "")[:256] or None,
        subscribers=_to_int(row.get(_COL_SUBS)),
        notice_amt=_to_int(row.get(_COL_AMT)),
        status_cd=(row.get(_COL_STATUS, "") or "")[:8] or None,
        resigned_at=(row.get(_COL_RESIGNED, "") or "")[:8] or None,
    )


def ingest_nps_rows(sm: sessionmaker[Session], rows) -> tuple[int, int]:
    """행 dict 이터러블을 스테이징 적재 후 원자 스왑한다. (적재행, 건너뜀) 반환.

    CSV 파일·odcloud API 페이지가 같은 경로를 탄다. 새 스냅샷을 pending=True 로
    스트리밍 적재(배치 커밋 — 수십만 행 메모리 안전)하고, **전량 성공했을 때만**
    한 트랜잭션에서 구 스냅샷 삭제 + pending 해제로 교체한다 — 소비 중 예외
    (API 5xx·키 만료 등)가 나면 활성 스냅샷은 무손상(적대 리뷰 H2). 0행이면
    교체하지 않는다(빈 응답이 스냅샷을 지우지 않게). 잔재 pending 은 시작 시 청소.
    """
    inserted = skipped = 0
    with sm() as session:  # 이전 실패 런의 스테이징 잔재 청소.
        session.execute(delete(NpsWorkplaceRow).where(NpsWorkplaceRow.pending.is_(True)))
        session.commit()
    batch: list[NpsWorkplaceRow] = []
    with sm() as session:
        for raw in rows:
            model = _row_to_model(raw)
            if model is None:
                skipped += 1
                continue
            model.pending = True
            batch.append(model)
            if len(batch) >= _BATCH:
                session.add_all(batch)
                session.commit()
                inserted += len(batch)
                batch = []
        if batch:
            session.add_all(batch)
            session.commit()
            inserted += len(batch)
    if inserted == 0:
        log.info("nps.ingest.empty", skipped=skipped)  # 활성 스냅샷 유지.
        return 0, skipped
    with sm() as session:  # 원자 스왑 — 단일 트랜잭션(둘 다 성공 또는 둘 다 무효).
        session.execute(delete(NpsWorkplaceRow).where(NpsWorkplaceRow.pending.is_(False)))
        session.execute(
            NpsWorkplaceRow.__table__.update()
            .where(NpsWorkplaceRow.pending.is_(True))
            .values(pending=False)
        )
        session.commit()
    log.info("nps.ingest", inserted=inserted, skipped=skipped)
    return inserted, skipped


def ingest_nps_csv(sm: sessionmaker[Session], path: str | Path) -> tuple[int, int]:
    """월간 CSV 파일을 통째 적재한다(스냅샷 교체) — :func:`ingest_nps_rows` 의 파일 어댑터."""
    p = Path(path)
    f, reader = _open_csv(p)
    try:
        return ingest_nps_rows(sm, reader)
    finally:
        f.close()


def map_industry_codes(sm: sessionmaker[Session], classifier) -> dict[str, int]:
    """스냅샷의 미등재 업종코드를 택소노미로 분류해 3층(ksic_industry_map)에 적재한다.

    ① 규칙 패스 — :func:`industry_from_ksic` (최장접두·동률 None·저정밀 차단이 이미
    들어있는 결정론 역매핑, 적대 검토 H2)로 풀리는 코드는 method='rule'.
    ② 잔여만 LLM — 닫힌 택소노미 분류기(classifier.classify)에 업종코드명 + 그 코드의
    대표 사업장명(가입자수 상위 3, 신뢰불가 원문이지만 분류기의 인젝션 프레이밍·닫힌집합
    게이트가 방어)을 근거로 넘긴다. abstain 은 '미분류'로 기록(닫힌 집합 밖 값 불가).
    미등재 코드만 처리(멱등 — 월간 재실행 시 신규 코드만 분류, 기존 재분류 0).
    """
    from ..sources.industry import industry_from_ksic
    from ..sources.taxonomy import UNCLASSIFIED

    stats = {"rule": 0, "llm": 0, "unclassified": 0, "already": 0}
    with sm() as session:
        rows = session.execute(
            select(
                NpsWorkplaceRow.industry_code,
                func.max(NpsWorkplaceRow.industry_name),
            )
            .where(NpsWorkplaceRow.pending.is_(False))
            .where(NpsWorkplaceRow.industry_code.is_not(None))
            .where(NpsWorkplaceRow.industry_code != "")
            .group_by(NpsWorkplaceRow.industry_code)
        ).all()
        existing = set(session.scalars(select(KsicIndustryMapRow.industry_code)).all())
    todo = [(c, n or "") for c, n in rows if c not in existing]
    stats["already"] = len(rows) - len(todo)

    for code, code_name in todo:
        label = industry_from_ksic(code)
        method = "rule"
        if label is None:
            method = "llm"
            with sm() as session:
                samples = session.scalars(
                    select(NpsWorkplaceRow.name)
                    .where(NpsWorkplaceRow.industry_code == code)
                    .where(NpsWorkplaceRow.pending.is_(False))
                    .order_by(NpsWorkplaceRow.subscribers.desc())
                    .limit(3)
                ).all()
            verdict = classifier.classify(
                name=f"업종: {code_name}",
                domain=None,
                text="이 업종(한국 표준산업분류 기반 업종코드 %s)의 대표 사업장: %s"
                % (code, ", ".join(samples)),
            )
            label = verdict.label  # 닫힌 집합 게이트 통과분만, abstain=None.
        if label is None:
            label = UNCLASSIFIED
            stats["unclassified"] += 1
        else:
            stats[method] += 1
        with sm() as session:
            if session.get(KsicIndustryMapRow, code) is None:  # 동시 실행 방어(멱등).
                session.add(
                    KsicIndustryMapRow(
                        industry_code=code,
                        industry_name=code_name[:256],
                        taxonomy_label=label,
                        method=method,
                    )
                )
                session.commit()
    log.info("nps.map_codes", **stats, total_codes=len(rows))
    return stats


class NpsStore:
    """발견 소스용 조회 어댑터 — 호출마다 자체 세션(스레드 안전, 다른 스토어와 동일 규약).

    조회 실패는 빈 결과 폴백(발견은 best-effort — 크롤 본체를 죽이지 않는다).
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def mapped_labels(self) -> frozenset[str]:
        """3층 매핑에 존재하는 택소노미 라벨 집합(미분류 제외) — 소스 게이팅용.

        NpsSource 가 인스턴스당 1회 로드해 인메모리로 게이트한다(applies_to 를
        세그먼트마다 DB 히트로 만들지 않기 위함 — 적대 검토 M2).
        """
        from ..sources.taxonomy import UNCLASSIFIED

        try:
            with self._factory() as session:
                labels = session.scalars(
                    select(KsicIndustryMapRow.taxonomy_label).distinct()
                ).all()
            return frozenset(lbl for lbl in labels if lbl and lbl != UNCLASSIFIED)
        except Exception as exc:
            log.info("nps.labels.error", err=str(exc))
            return frozenset()

    def page_by_label(self, label: str, *, offset: int, limit: int) -> list[NpsWorkplaceRow]:
        """3층 매핑 라벨로 페이지 조회 — 접두 매칭을 대체하는 통합층 경로.

        정렬·탈퇴 제외·pending 제외는 :meth:`page` 와 동일 계약.
        """
        if not label:
            return []
        try:
            with self._factory() as session:
                code_sq = select(KsicIndustryMapRow.industry_code).where(
                    KsicIndustryMapRow.taxonomy_label == label
                )
                stmt = (
                    select(NpsWorkplaceRow)
                    .where(NpsWorkplaceRow.pending.is_(False))
                    .where(NpsWorkplaceRow.industry_code.in_(code_sq))
                    .where(
                        (NpsWorkplaceRow.resigned_at.is_(None))
                        | (NpsWorkplaceRow.resigned_at == "")
                    )
                    .order_by(NpsWorkplaceRow.subscribers.desc(), NpsWorkplaceRow.id.asc())
                    .offset(offset)
                    .limit(limit)
                )
                return list(session.scalars(stmt).all())
        except Exception as exc:
            log.info("nps.page_label.error", err=str(exc))
            return []

    def count(self) -> int:
        try:
            with self._factory() as session:
                return (
                    session.query(NpsWorkplaceRow)
                    .filter(NpsWorkplaceRow.pending.is_(False))
                    .count()
                )
        except Exception as exc:
            log.info("nps.count.error", err=str(exc))
            return 0

    def page(
        self, prefixes: tuple[str, ...], *, offset: int, limit: int
    ) -> list[NpsWorkplaceRow]:
        """업종 접두 매칭 + 미탈퇴 사업장을 가입자수 내림차순으로 페이지 조회한다.

        정렬 (subscribers DESC, id ASC) 는 스냅샷 내 결정적 — 세그먼트 커서(offset)의
        기준. 스냅샷 교체(월간) 후엔 순서가 바뀔 수 있으나 dedup(제약①)이 재발견을
        걸러 자가치유된다(등록처 커서와 동일 계약).
        """
        if not prefixes:
            return []
        try:
            with self._factory() as session:
                cond = [NpsWorkplaceRow.industry_code.like(f"{p}%") for p in prefixes]
                stmt = (
                    select(NpsWorkplaceRow)
                    .where(NpsWorkplaceRow.pending.is_(False))  # 스테이징 중 행 제외.
                    .where(or_(*cond))
                    .where(
                        (NpsWorkplaceRow.resigned_at.is_(None))
                        | (NpsWorkplaceRow.resigned_at == "")
                    )
                    .order_by(NpsWorkplaceRow.subscribers.desc(), NpsWorkplaceRow.id.asc())
                    .offset(offset)
                    .limit(limit)
                )
                return list(session.scalars(stmt).all())
        except Exception as exc:
            log.info("nps.page.error", err=str(exc))
            return []
