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

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, sessionmaker

from ..logging import get_logger
from ..schema import NpsWorkplaceRow

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
            continue
    raise ValueError(f"CSV 헤더에서 '{_COL_NAME}' 컬럼을 찾지 못함(인코딩/서식 확인): {path}")


def ingest_nps_csv(sm: sessionmaker[Session], path: str | Path) -> tuple[int, int]:
    """월간 CSV 를 통째 적재한다(기존 스냅샷 전체 교체). (적재행, 건너뜀) 반환.

    스냅샷 교체 = 단순·멱등(같은 파일 재실행 결과 동일). 사업장명 없는 행은 건너뛴다.
    """
    p = Path(path)
    f, reader = _open_csv(p)
    inserted = skipped = 0
    try:
        with sm() as session:
            session.execute(delete(NpsWorkplaceRow))
            session.commit()
        batch: list[NpsWorkplaceRow] = []
        with sm() as session:
            for raw in reader:
                row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
                name = row.get(_COL_NAME, "")
                if not name:
                    skipped += 1
                    continue
                bizno = row.get(_COL_BIZNO, "").replace("-", "")[:6] or None
                addr = row.get(_COL_ADDR_ROAD) or row.get(_COL_ADDR_JIBUN) or None
                batch.append(
                    NpsWorkplaceRow(
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
                )
                if len(batch) >= _BATCH:
                    session.add_all(batch)
                    session.commit()
                    inserted += len(batch)
                    batch = []
            if batch:
                session.add_all(batch)
                session.commit()
                inserted += len(batch)
    finally:
        f.close()
    log.info("nps.ingest", path=str(p), inserted=inserted, skipped=skipped)
    return inserted, skipped


class NpsStore:
    """발견 소스용 조회 어댑터 — 호출마다 자체 세션(스레드 안전, 다른 스토어와 동일 규약).

    조회 실패는 빈 결과 폴백(발견은 best-effort — 크롤 본체를 죽이지 않는다).
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def count(self) -> int:
        try:
            with self._factory() as session:
                return session.query(NpsWorkplaceRow).count()
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
