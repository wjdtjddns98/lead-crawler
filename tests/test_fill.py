"""이메일 채우기 consumer(pipeline.fill) — 빈 대상 조기반환 + 카운트(네트워크 없음)."""

from __future__ import annotations

from leadcrawler.config import Settings
from leadcrawler.pipeline.fill import count_targets, fill_batch


class _Result:
    def all(self):
        return []

    def scalar(self):
        return 0


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return _Result()


def _sm():
    return _Session()


def test_fill_batch_empty_targets_noop() -> None:
    # 대상 0건이면 컴포넌트도 안 만들고 (0,0) 조기반환 — 네트워크·enrich 없음.
    assert fill_batch(Settings(dry_run=False), _sm, limit=50, workers=4) == (0, 0)


def test_count_targets_reads_scalar() -> None:
    assert count_targets(_sm) == 0


def test_count_targets_country_scope(tmp_path) -> None:
    """국가 스코프 — 크롤이 국가 명시선택이면 채우기 대상도 그 국가만 센다('대한민국' 별칭 포함)."""
    from leadcrawler.schema import CompanyRow, DiscoveredCompanyRow
    from leadcrawler.storage.db import get_sessionmaker, init_db

    s = Settings(database_url=f"sqlite:///{tmp_path}/fc.db", dry_run=False)
    init_db(s)
    sm = get_sessionmaker(s)
    with sm() as session:
        rows = [
            ("dom:kr:a.co.kr", "한국사", "대한민국", "a.co.kr"),
            ("dom:us:b.com", "USCo", "US", "b.com"),
        ]
        for key, name, country, domain in rows:
            session.add(DiscoveredCompanyRow(
                canonical_key=key, name=name, country=country, industry="화학·석유화학",
                source="import", domain=domain,
            ))
        session.commit()  # FK(company→discovered) 순서 보장 — 발견행 먼저 확정.
        for i, (key, name, country, _domain) in enumerate(rows):
            session.add(CompanyRow(
                id=f"co_{i}", canonical_key=key, name=name, country=country,
                industry="화학·석유화학", site_alive=True,
            ))
        session.commit()

    assert count_targets(sm) == 2  # 무스코프=전세계(현행).
    assert count_targets(sm, ["US"]) == 1  # KR('대한민국' 표기) 제외.
    assert count_targets(sm, ["KR"]) == 1  # 'KR' 선택이 '대한민국' 표기도 잡는다(별칭 확장).


def test_count_targets_industry_include(tmp_path) -> None:
    """--industry include — 지정 업종만 센다(굶는 세그먼트 타겟 보충, 2026-08-19 P1).

    '미분류' 는 라벨 빈값 행과 대칭 매칭 — /queue/stock 뱃지 값을 그대로 타겟으로
    옮겨 쓸 수 있는 어휘 계약(#360 선례).
    """
    from leadcrawler.schema import CompanyRow, DiscoveredCompanyRow
    from leadcrawler.sources.taxonomy import UNCLASSIFIED
    from leadcrawler.storage.db import get_sessionmaker, init_db

    s = Settings(database_url=f"sqlite:///{tmp_path}/fi.db", dry_run=False)
    init_db(s)
    sm = get_sessionmaker(s)
    rows = [
        ("dom:kr:sec.co.kr", "보안사", "정보보안"),
        ("dom:kr:game.co.kr", "게임사", "게임"),
        ("dom:kr:none.co.kr", "무업종사", ""),  # 라벨 빈값 — '미분류' 로만 타겟 가능.
    ]
    with sm() as session:
        for key, name, industry in rows:
            session.add(DiscoveredCompanyRow(
                canonical_key=key, name=name, country="KR", industry=industry,
                source="import", domain=key.split(":")[-1],
            ))
        session.commit()  # FK(company→discovered) 순서 보장.
        for i, (key, name, industry) in enumerate(rows):
            session.add(CompanyRow(
                id=f"ci_{i}", canonical_key=key, name=name, country="KR",
                industry=industry, site_alive=True,
            ))
        session.commit()

    assert count_targets(sm, industries=["정보보안"]) == 1
    assert count_targets(sm, industries=["정보보안", "게임"]) == 2
    assert count_targets(sm, industries=[UNCLASSIFIED]) == 1  # 빈값 행 대칭 매칭.
    assert count_targets(sm, industries=["정보보안"], exclude_industries=["정보보안"]) == 0
    assert count_targets(sm) == 3  # 미지정=전체(현행 유지).


def test_count_targets_exclude_filters(tmp_path) -> None:
    """업종 제외·상장 제외 필터 — 국가 스코프와 조합돼야 한다(KR 비상장 백필 경로)."""
    from leadcrawler.schema import CompanyRow, DiscoveredCompanyRow
    from leadcrawler.storage.db import get_sessionmaker, init_db

    s = Settings(database_url=f"sqlite:///{tmp_path}/fx.db", dry_run=False)
    init_db(s)
    sm = get_sessionmaker(s)
    #        key                  업종             listed
    rows = [
        ("dom:kr:a.co.kr", "화학·석유화학", "unlisted"),
        ("dom:kr:b.co.kr", "식품·음료", "unlisted"),   # 업종 제외 대상.
        ("dom:kr:c.co.kr", "화학·석유화학", "listed"),  # 상장 제외 대상.
        ("dom:kr:d.co.kr", "", "unknown"),               # 업종 빈값·unknown — 대상 유지.
    ]
    with sm() as session:
        for key, industry, listed in rows:
            session.add(DiscoveredCompanyRow(
                canonical_key=key, name=key, country="KR", industry=industry,
                listed=listed, source="import", domain=key.split(":")[-1],
            ))
        session.commit()  # FK 순서 — 발견행 먼저.
        for i, (key, industry, _listed) in enumerate(rows):
            session.add(CompanyRow(
                id=f"co_{i}", canonical_key=key, name=key, country="KR",
                industry=industry, site_alive=True,
            ))
        session.commit()

    assert count_targets(sm, ["KR"]) == 4  # 필터 없음=전부.
    assert count_targets(sm, ["KR"], exclude_industries=["식품·음료"]) == 3
    assert count_targets(sm, ["KR"], exclude_listed=True) == 3  # unknown 은 남는다.
    assert count_targets(
        sm, ["KR"], exclude_industries=["식품·음료", "게임"], exclude_listed=True
    ) == 2  # a + d 만.


def test_count_resolve_targets_exclude_filters(tmp_path) -> None:
    """resolve 경로 필터 — 미승격(co 없음) 행은 발견 라벨(d.industry) 폴백으로 제외한다."""
    from leadcrawler.pipeline.fill import count_resolve_targets
    from leadcrawler.schema import DiscoveredCompanyRow
    from leadcrawler.storage.db import get_sessionmaker, init_db

    s = Settings(database_url=f"sqlite:///{tmp_path}/rx.db", dry_run=False)
    init_db(s)
    sm = get_sessionmaker(s)
    rows = [
        ("nm:kr:가", "화학·석유화학", "unlisted"),
        ("nm:kr:나", "식품·음료", "unlisted"),  # 업종 제외 대상(발견 라벨).
        ("nm:kr:다", "화학·석유화학", "listed"),  # 상장 제외 대상.
        ("nm:kr:라", "", "unknown"),              # 빈 라벨·unknown — 대상 유지.
    ]
    with sm() as session:
        for key, industry, listed in rows:
            session.add(DiscoveredCompanyRow(
                canonical_key=key, name=key, country="KR", industry=industry,
                listed=listed, source="nps", domain=None,  # 도메인 없음 = resolve 대상.
            ))
        # dedup 흡수행(duplicate_of)은 도메인이 없어도 해석 대상이 아니다(2026-09-01).
        session.add(DiscoveredCompanyRow(
            canonical_key="nm:kr:마", name="마", country="KR", industry="화학·석유화학",
            listed="unlisted", source="nps", domain=None, duplicate_of="nm:kr:가",
        ))
        session.commit()

    assert count_resolve_targets(sm, ["KR"]) == 4
    # 대상 SQL 도 같은 가드를 가진다(카운트만 막고 선택은 새는 회귀 방지).
    from leadcrawler.pipeline.fill import _RESOLVE_TARGET_SQL, _scoped
    stmt, params = _scoped(_RESOLVE_TARGET_SQL, ["KR"])
    with sm() as session:
        picked = {r.canonical_key for r in session.execute(stmt, {**params, "limit": 10})}
    assert picked == {"nm:kr:가", "nm:kr:나", "nm:kr:다", "nm:kr:라"}
    assert count_resolve_targets(sm, ["KR"], exclude_industries=["식품·음료"]) == 3
    assert count_resolve_targets(sm, ["KR"], exclude_listed=True) == 3
    assert count_resolve_targets(
        sm, ["KR"], exclude_industries=["식품·음료"], exclude_listed=True
    ) == 2  # 가 + 라 만.


def test_fill_batch_advances_past_emailless_rows(tmp_path, monkeypatch) -> None:
    """이메일을 못 찾은 회사가 대기열 선두를 막지 않는다 — 배치마다 다음 구간을 잡아야 한다.

    회귀 가드(2026-07-31 실사고): 대상은 '이메일 없는 회사'라 못 찾으면 대상에서 안 빠지고,
    구 정렬 `order by co.id` 는 매번 같은 앞머리를 돌려줬다(800건 처리에 대기열 9 감소 —
    찾은 이메일 수와 정확히 일치). 처리하면 last_crawled_at 이 밀려 뒤로 가야 한다.
    """
    from leadcrawler.pipeline import fill as fill_mod
    from leadcrawler.schema import CompanyRow, DiscoveredCompanyRow
    from leadcrawler.storage.db import get_sessionmaker, init_db
    from leadcrawler.storage.repository import company_id_for

    s = Settings(database_url=f"sqlite:///{tmp_path}/adv.db", dry_run=False)
    init_db(s)
    sm = get_sessionmaker(s)
    keys = [f"dom:kr:x{i}.co.kr" for i in range(3)]
    with sm() as session:
        for k in keys:
            session.add(DiscoveredCompanyRow(
                canonical_key=k, name=k, country="KR", industry="화학·석유화학",
                source="nps", domain=k.split(":")[-1],
            ))
        session.commit()  # FK 순서 — 발견행 먼저.
        for k in keys:
            # id 는 save_lead 와 같은 규칙으로 — 임의값이면 재저장이 UNIQUE 충돌로
            # 롤백돼(persist.skip.conflict) last_crawled_at 갱신까지 날아간다.
            session.add(CompanyRow(
                id=company_id_for(k), canonical_key=k, name=k, country="KR",
                industry="화학·석유화학", site_alive=True,
            ))
        session.commit()

    # enrich 는 이메일을 못 찾는다(대상에서 안 빠지는 상황 재현) — 네트워크 0.
    class _NoEmailEnricher:
        def __init__(self, settings, *, cost_ledger=None, **_kw):
            self.last_home_html = None
            self.last_home_rendered_html = None

        def enrich(self, dc):
            return []

        def close(self):
            pass

    class _Alive:
        def __init__(self, settings, *, registry_checker=None, **_kw):
            pass

        def verify(self, domain, **_kw):
            from leadcrawler.verify.existence import ExistenceResult

            return ExistenceResult(is_active=True, site_alive=True, confidence=0.9)

        def close(self):
            pass

    class _Val:
        def __init__(self, settings, *, cost_ledger=None, **_kw):
            self.settings = settings

        def validate(self, email, company_domain=None, *, deep=True):
            from leadcrawler.models import EmailValidation

            return EmailValidation()

        def close(self):
            pass

    monkeypatch.setattr(fill_mod, "Enricher", _NoEmailEnricher)
    monkeypatch.setattr(fill_mod, "ExistenceVerifier", _Alive)
    monkeypatch.setattr(fill_mod, "EmailValidator", _Val)
    monkeypatch.setattr(fill_mod, "build_classifier", lambda settings, **kw: None)
    monkeypatch.setattr(fill_mod, "build_registry_checker", lambda settings, **kw: None)

    seen: list[str] = []
    original = fill_mod._dc_from_row

    def _spy(r):
        dc = original(r)
        seen.append(dc.canonical_key)
        return dc

    monkeypatch.setattr(fill_mod, "_dc_from_row", _spy)

    for _ in range(3):  # limit=1 로 3번 — 매번 다른 회사를 잡아야 한다.
        processed, emails = fill_mod.fill_batch(s, sm, limit=1, workers=1)
        assert (processed, emails) == (1, 0)  # 이메일은 계속 0.

    assert sorted(seen) == sorted(keys), f"같은 회사를 반복 처리함: {seen}"


def test_stall_watchdog_fires_on_stall() -> None:
    """진행(beat) 없이 stall_s 를 넘기면 주입된 _exit 이 종료코드와 함께 호출된다."""
    import time

    from leadcrawler.pipeline.fill import _STALL_EXIT_CODE, _StallWatchdog

    calls: list[int] = []
    with _StallWatchdog("t", 0.2, _exit=calls.append):
        deadline = time.monotonic() + 3.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
    assert calls == [_STALL_EXIT_CODE]


def test_stall_watchdog_beat_prevents_exit() -> None:
    """소비 루프가 beat 을 계속 치면 정체로 판정하지 않는다."""
    import time

    from leadcrawler.pipeline.fill import _StallWatchdog

    calls: list[int] = []
    with _StallWatchdog("t", 0.4, _exit=calls.append) as wd:
        for _ in range(10):
            time.sleep(0.05)
            wd.beat()
    assert calls == []


def test_stall_watchdog_disabled_when_none() -> None:
    """stall_s=None(웹서버 background 경로)이면 감시 스레드 자체를 안 띄운다."""
    from leadcrawler.pipeline.fill import _StallWatchdog

    calls: list[int] = []
    with _StallWatchdog("t", None, _exit=calls.append) as wd:
        assert wd._thread.ident is None  # 한 번도 start 안 됨(미기동 ≠ 이미 종료).
    assert calls == []


def test_stall_watchdog_child_enum_failure_still_exits(monkeypatch) -> None:
    """자식 열거(PowerShell)가 죽어도 최종 _exit(86) 은 반드시 호출된다(리뷰 MED 잠금)."""
    import subprocess
    import time

    from leadcrawler.pipeline.fill import _STALL_EXIT_CODE, _StallWatchdog

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise OSError("powershell unavailable")

    monkeypatch.setattr(subprocess, "run", boom)
    calls: list[int] = []
    with _StallWatchdog("t", 0.2, _exit=calls.append, _kill_children=True):
        deadline = time.monotonic() + 3.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
    assert calls == [_STALL_EXIT_CODE]


def test_stall_watchdog_kills_each_enumerated_child(monkeypatch) -> None:
    """열거된 자식 PID 마다 taskkill /T 1회 — 죽음 경로의 유일한 실행 검증(리뷰 MED)."""
    import subprocess
    import time
    from types import SimpleNamespace

    from leadcrawler.pipeline.fill import _STALL_EXIT_CODE, _StallWatchdog

    killed: list[str] = []

    def fake_run(cmd, **k):  # noqa: ANN001, ANN003
        if cmd[0] == "taskkill":
            killed.append(cmd[4])
        return SimpleNamespace(stdout="123\n456\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    calls: list[int] = []
    with _StallWatchdog("t", 0.2, _exit=calls.append, _kill_children=True):
        deadline = time.monotonic() + 3.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
    assert killed == ["123", "456"]
    assert calls == [_STALL_EXIT_CODE]


def test_stall_default_is_off_for_library_callers() -> None:
    """계약: 배치 함수 기본값 = 감시 끔 — 웹서버(background.py)가 안 넘기는 한 절대
    os._exit 이 서버에서 발화하지 않는다. 기본값이 바뀌면 서버가 죽을 수 있다(리뷰 MED)."""
    import inspect

    from leadcrawler.pipeline.fill import fill_batch, resolve_batch

    assert inspect.signature(fill_batch).parameters["stall_exit_s"].default is None
    assert inspect.signature(resolve_batch).parameters["stall_exit_s"].default is None


def test_cli_stall_option_mapping(monkeypatch) -> None:
    """계약: CLI 기본(OptionInfo)→900, 0→None(끔) 으로 배치 함수에 전달된다."""
    from types import SimpleNamespace

    import leadcrawler.cli as cli
    import leadcrawler.pipeline.fill as fill_mod
    import leadcrawler.storage.db as db

    monkeypatch.setattr(cli, "get_settings", lambda: SimpleNamespace(dry_run=False))
    monkeypatch.setattr(cli, "_open_run", lambda s: None)
    monkeypatch.setattr(db, "get_sessionmaker", lambda s: object())
    monkeypatch.setattr(cli, "_acquire_track_lock_or_exit", lambda *a, **k: object())
    monkeypatch.setattr(fill_mod, "count_targets", lambda sm, countries=None, **kw: 100)
    seen: dict = {}

    def fake_fill_batch(settings, sm, *, limit, workers, countries=None, **kw):
        seen.update(kw)
        return 1, 0

    monkeypatch.setattr(fill_mod, "fill_batch", fake_fill_batch)
    cli.fill_emails(loop=True, batch=5, workers=1, interval=0.0, min_queue=0, max_batches=1)
    assert seen["stall_exit_s"] == 900.0  # OptionInfo 직접호출 → 기본 900.
    cli.fill_emails(
        loop=True, batch=5, workers=1, interval=0.0, min_queue=0, max_batches=1,
        stall_exit_secs=0,
    )
    assert seen["stall_exit_s"] is None  # 0 = 끔.
