"""운영비 원장 — 유료 외부 호출의 과금을 추적·집계하고 월 예산 초과 시 차단한다.

연결점: EmailAPI(Hunter/Apollo)·Vision(Claude)·딜리버러빌리티(ZeroBounce/NeverBounce)
같은 **호출당 과금** 외부 연동. 각 호출부가 :meth:`CostLedger.record` 로 1건씩 적재하고,
:meth:`CostLedger.is_over_budget` 로 월 누계가 ``monthly_budget_krw`` 를 넘었는지 본다.

- **영속(persist=True)**: ``cost_ledger`` 테이블에 행을 적재하고 월 누계를 DB 에서 집계
  (재크롤·다중 프로세스 누계 합산). 호출당 짧은 세션을 열고 닫아 파이프라인의 리드
  트랜잭션과 격리한다(유료 호출은 opt-in·드물어 비용 무시 가능).
- **인메모리(기본)**: 세션 없이 현재 프로세스 누계만 — 테스트·비영속 실행용.
- 단가는 :data:`DEFAULT_PRICING_KRW`(호출당 원) 기본값을 생성자 ``pricing`` 으로 덮어쓴다.
  실제 청구와 정확히 일치하진 않는 **보수적 추정치**다(예산 가드 목적).

dry_run·무료 경로는 호출 자체가 없어 원장에 0원 행을 남기지 않는다(실제 과금만 기록).

**예산 가드는 best-effort soft cap 이다.** ① 가드는 유료 호출 직전마다 누계를 보지만
gate↔호출 사이에 락이 없어, 다중 워커가 동시에 "예산 내"를 읽고 각자 발사하면 최대
``(동시 워커 수 × 호출당 단가)`` 만큼 초과할 수 있다. ② 단가(:data:`DEFAULT_PRICING_KRW`)
는 실제 청구가 아닌 보수적 추정치라, 실단가가 추정보다 크면 가드가 늦게 걸린다. 월 50만원
한도·호출당 10~50원·opt-in 희소 호출 규모에선 허용 가능한 근사다(엄격한 상한이 필요하면
단가를 실청구로 보정하고 행 단위 원자적 차감으로 강화).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Protocol
from uuid import uuid4

from .config import Settings, get_settings
from .logging import get_logger
from .models import CostEvent

log = get_logger("cost")

# 유료 호출 1건당 추정 단가(원). provider 명은 호출부 식별자와 동일해야 한다.
# 실제 청구 기준이 아닌 예산 가드용 보수적 기본값 — 운영 중 생성자로 보정한다.
DEFAULT_PRICING_KRW: dict[str, int] = {
    "hunter": 50,  # Hunter domain-search 요청(크레딧)
    "apollo": 50,  # Apollo people-search 요청(크레딧)
    "zerobounce": 10,  # ZeroBounce 이메일 1건 검증
    "neverbounce": 10,  # NeverBounce 이메일 1건 검증
    "vision": 30,  # Claude Vision 이미지 1장(저가 모델)
}


def month_key_of(dt: datetime) -> str:
    """집계 키 YYYY-MM 을 반환한다."""
    return dt.strftime("%Y-%m")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SupportsCostLedger(Protocol):
    """호출부가 의존하는 최소 원장 인터페이스(테스트 더블이 구현)."""

    def record(self, provider: str, units: int = 1) -> CostEvent:
        """과금 1건을 적재한다."""
        ...

    def is_over_budget(self) -> bool:
        """이번 달 누계가 예산 이상인지."""
        ...


class CostLedger:
    """유료 호출 과금 원장(영속 또는 인메모리)."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        persist: bool = False,
        pricing: dict[str, int] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._persist = persist
        self._pricing = {**DEFAULT_PRICING_KRW, **(pricing or {})}
        self._now = now or _utcnow
        self._mem: list[CostEvent] = []  # 인메모리 모드 누계

    def unit_cost(self, provider: str) -> int:
        """provider 의 호출당 단가(원). 미등록 provider 는 0(과금 미추적)."""
        return int(self._pricing.get(provider, 0))

    def record(self, provider: str, units: int = 1) -> CostEvent:
        """유료 호출 1건의 과금을 적재하고 :class:`CostEvent` 를 반환한다."""
        units = max(0, int(units))
        now = self._now()
        unit = self.unit_cost(provider)
        if unit == 0:  # 단가 미등록 — 실제 과금은 났는데 0원 집계되면 예산 가드가 샌다.
            log.warning("cost.unpriced_provider", provider=provider)
        ev = CostEvent(
            provider=provider,
            units=units,
            unit_cost_krw=unit,
            cost_krw=unit * units,
            occurred_at=now,
            month_key=month_key_of(now),
        )
        if self._persist:
            self._persist_row(ev)
        else:
            self._mem.append(ev)
        log.info(
            "cost.record", provider=provider, units=units, cost_krw=ev.cost_krw, month=ev.month_key
        )
        return ev

    def month_total_krw(self, month_key: str | None = None) -> int:
        """해당 월(기본=이번 달 UTC) 과금 누계(원)."""
        key = month_key or month_key_of(self._now())
        if self._persist:
            from sqlalchemy import func, select

            from .schema import CostLedgerRow
            from .storage.db import get_sessionmaker

            session = get_sessionmaker(self.settings)()
            try:
                total = session.execute(
                    select(func.coalesce(func.sum(CostLedgerRow.cost_krw), 0)).where(
                        CostLedgerRow.month_key == key
                    )
                ).scalar_one()
                return int(total or 0)
            finally:
                session.close()
        return sum(e.cost_krw for e in self._mem if e.month_key == key)

    def breakdown(self, month_key: str | None = None) -> dict[str, int]:
        """해당 월 provider 별 과금 누계(원) — 큰 순서로 정렬된 dict."""
        key = month_key or month_key_of(self._now())
        totals: dict[str, int] = {}
        if self._persist:
            from sqlalchemy import func, select

            from .schema import CostLedgerRow
            from .storage.db import get_sessionmaker

            session = get_sessionmaker(self.settings)()
            try:
                rows = session.execute(
                    select(CostLedgerRow.provider, func.sum(CostLedgerRow.cost_krw))
                    .where(CostLedgerRow.month_key == key)
                    .group_by(CostLedgerRow.provider)
                ).all()
                totals = {str(p): int(c or 0) for p, c in rows}
            finally:
                session.close()
        else:
            for e in self._mem:
                if e.month_key == key:
                    totals[e.provider] = totals.get(e.provider, 0) + e.cost_krw
        return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))

    def remaining_krw(self, month_key: str | None = None) -> int:
        """이번 달 남은 예산(원). 음수면 0(이미 초과)."""
        return max(0, self.settings.monthly_budget_krw - self.month_total_krw(month_key))

    def is_over_budget(self, month_key: str | None = None) -> bool:
        """이번 달 누계가 예산 이상인지(차단 게이트 기준)."""
        return self.month_total_krw(month_key) >= self.settings.monthly_budget_krw

    def _persist_row(self, ev: CostEvent) -> None:
        """과금 1건을 cost_ledger 에 적재한다(격리된 짧은 세션).

        DB 쓰기 실패는 **삼키고 loud warning** 만 남긴다 — 과금 추적 실패가 크롤 배치
        전체를 죽이지 않게(파이프라인 리드 트랜잭션과 격리). 단, 추적이 누락되면 이후
        ``month_total_krw`` 가 과소집계되어 예산 가드가 잠깐 눈멀 수 있으므로 warning 으로
        운영자가 인지하게 한다(info 아님).
        """
        from .schema import CostLedgerRow
        from .storage.db import get_sessionmaker

        try:
            session = get_sessionmaker(self.settings)()
            try:
                session.add(
                    CostLedgerRow(
                        id=uuid4().hex[:12],
                        provider=ev.provider,
                        units=ev.units,
                        unit_cost_krw=ev.unit_cost_krw,
                        cost_krw=ev.cost_krw,
                        occurred_at=ev.occurred_at,
                        month_key=ev.month_key,
                    )
                )
                session.commit()
            finally:
                session.close()
        except Exception as exc:  # DB 장애 → 추적 degrade(파이프라인 생존), 운영자 가시화.
            log.warning(
                "cost.persist.error", provider=ev.provider, cost_krw=ev.cost_krw, err=str(exc)
            )
