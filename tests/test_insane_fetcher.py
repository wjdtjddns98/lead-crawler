"""InsaneFetcher 어댑터(A3) — 우회 엔진을 SupportsFetch 로 감싸기(네트워크/엔진 없음)."""

from __future__ import annotations

import pytest

from leadcrawler.sources.insane_fetcher import InsaneFetcher


class _Result:
    """엔진 FetchResult 더블(ok, content 덕타이핑)."""

    def __init__(self, ok: bool, content: str = "") -> None:
        self.ok = ok
        self.content = content


def _fn(result, *, record=None):
    def inner(url, **kw):
        if record is not None:
            record.append((url, kw))
        return result
    return inner


def test_ok_returns_content() -> None:
    f = InsaneFetcher(fetch_fn=_fn(_Result(True, "<html>회사목록</html>")))
    assert f.available is True
    assert f.get_text("https://set.or.th/x") == "<html>회사목록</html>"


def test_blocked_returns_empty() -> None:
    f = InsaneFetcher(fetch_fn=_fn(_Result(False, "challenge")))
    assert f.get_text("https://x") == ""  # ok=False → graceful 빈 결과


def test_engine_exception_is_graceful() -> None:
    def boom(url, **kw):
        raise RuntimeError("curl_cffi exploded")

    assert InsaneFetcher(fetch_fn=boom).get_text("https://x") == ""


def test_no_engine_is_noop() -> None:
    # fetch_fn 미주입 + 엔진 미가용 가정 시 available False, 모든 GET 빈 결과.
    f = InsaneFetcher(fetch_fn=None)
    # _default_fetch_fn 이 엔진을 찾으면 available True 일 수 있으나, 없으면 False·빈 결과.
    if not f.available:
        assert f.get_text("https://x") == ""


def test_params_appended_to_url() -> None:
    rec: list = []
    f = InsaneFetcher(fetch_fn=_fn(_Result(True, "ok"), record=rec))
    f.get_text("https://x/list", params={"page": 2})
    assert rec[0][0] == "https://x/list?page=2"


def test_get_bytes_encodes_text() -> None:
    f = InsaneFetcher(fetch_fn=_fn(_Result(True, "héllo")))
    assert f.get_bytes("https://x") == "héllo".encode("utf-8")


def test_get_json_parses_and_empty_raises() -> None:
    ok = InsaneFetcher(fetch_fn=_fn(_Result(True, '{"a": 1}')))
    assert ok.get_json("https://x") == {"a": 1}
    blocked = InsaneFetcher(fetch_fn=_fn(_Result(False)))
    with pytest.raises(RuntimeError):
        blocked.get_json("https://x")  # 빈/차단 → 예외(호출부 graceful 처리)


def test_post_not_supported() -> None:
    f = InsaneFetcher(fetch_fn=_fn(_Result(True, "x")))
    with pytest.raises(NotImplementedError):
        f.post_text("https://x")
    with pytest.raises(NotImplementedError):
        f.post_json("https://x")
