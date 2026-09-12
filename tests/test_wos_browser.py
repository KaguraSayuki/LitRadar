"""WoS 浏览器导出器的离线 fake-browser 回归测试。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from litradar.sources import wos_browser


class FakeDownload:
    def __init__(self, path: Path):
        self._path = path

    def path(self):
        return self._path


class FakeLocator:
    def __init__(self, page, selector: str, *, text: str = "", count: int = 1,
                 scope: str | None = None):
        self.page = page
        self.selector = selector
        self.text = text
        self._count = count
        self.scope = scope
        self.first = self
        self.last = self

    def count(self):
        return self._count

    def is_visible(self):
        return self._count > 0

    def wait_for(self, **kwargs):
        self.page.actions.append(("wait", self.selector, kwargs))

    def inner_text(self, **kwargs):
        return self.text

    def click(self, **kwargs):
        if self._count == 0:
            raise RuntimeError(f"missing locator: {self.selector}")
        self.page.actions.append(("click", self.selector, kwargs))
        if self.selector == "#exportToRisButton":
            self.page.dialog_open = True
        elif self.selector == "#exportButton":
            self.page.dialog_open = False

    def check(self, **kwargs):
        if self._count == 0:
            raise RuntimeError(f"missing locator: {self.selector}")
        self.page.actions.append(("check", self.selector, kwargs))

    def fill(self, value, **kwargs):
        if self._count == 0:
            raise RuntimeError(f"missing locator: {self.selector}")
        self.page.actions.append(("fill", self.selector, str(value), kwargs))

    def locator(self, selector):
        return self.page.locator(selector, scope=self.scope)

    def get_by_role(self, role, name=None, exact=False):
        return self.page.get_by_role(role, name=name, exact=exact,
                                     scope=self.scope)

    def get_by_label(self, text, exact=False):
        return self.page.get_by_label(text, exact=exact, scope=self.scope)


class FakeExpectDownload:
    def __init__(self, page):
        self.page = page
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.value = self.page.downloads.pop(0)
        return False


class FakePage:
    def __init__(self, total: int, downloads: list[FakeDownload], *,
                 total_text: str | None = None):
        self.url = ("https://www.webofscience.com/wos/woscc/"
                    "alert-execution-summary/00000000-0000-0000-0000-"
                    "000000000001")
        self.total = total
        self.total_text = total_text or f"{self.total:,} alerting results for:"
        self.downloads = downloads
        self.actions = []
        self.dialog_open = False

    def goto(self, url, **kwargs):
        self.actions.append(("goto", url, kwargs))

    def locator(self, selector, *, scope=None):
        if selector == "#GenericFD-search-searchInfo-parent":
            return FakeLocator(self, selector, text=self.total_text)
        if selector.startswith("#onetrust-"):
            return FakeLocator(self, selector, count=0)
        if selector == "#radio3-input" and scope != "dialog":
            return FakeLocator(self, selector, count=0, scope=scope)
        if selector in {"#mat-input-0", "#mat-input-1", "#mat-input-2",
                        "#mat-input-3"}:
            return FakeLocator(self, selector, count=0, scope=scope)
        # Angular attaches the option panel to the document rather than the
        # dialog, so this remains a page-level locator.
        if selector == "#option-authorTitleSourceAbstract":
            return FakeLocator(self, selector)
        return FakeLocator(self, selector, scope=scope)

    def get_by_role(self, role, name=None, exact=False, scope=None):
        selector = f"role={role}:{name or ''}"
        if role == "dialog":
            return FakeLocator(self, selector, count=int(self.dialog_open),
                               scope="dialog")
        if role == "textbox":
            labels = {
                "Input starting record range",
                ("Input ending record range. A maximum of 1000 records can be "
                 "exported at one time."),
            }
            if scope != "dialog" or not any(
                    (name.search(label) if hasattr(name, "search") else
                     name == label)
                    for label in labels):
                return FakeLocator(self, selector, count=0, scope=scope)
        if role == "combobox":
            if scope != "dialog" or not (hasattr(name, "search") and
                                          name.search("Filter by, Author")):
                return FakeLocator(self, selector, count=0, scope=scope)
        return FakeLocator(self, selector, scope=scope)

    def get_by_label(self, text, exact=False, scope=None):
        selector = f"label={text}"
        labels = {
            "Input starting record range",
            ("Input ending record range. A maximum of 1000 records can be "
             "exported at one time."),
        }
        return FakeLocator(self, selector,
                           count=int(scope == "dialog" and text in labels),
                           scope=scope)

    def expect_download(self, **kwargs):
        return FakeExpectDownload(self)


class FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False
        self.default_timeout = None

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, context):
        self.context = context
        self.calls = []

    def launch_persistent_context(self, profile, **kwargs):
        self.calls.append((profile, kwargs))
        return self.context


class FakeSync:
    def __init__(self, context):
        self.chromium = FakeChromium(context)
        self.stopped = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stopped = True
        return False


def _ris(start: int, count: int, *, bom: bool = False) -> bytes:
    body = b"".join(
        f"TY  - JOUR\nTI  - Paper {i}\nER  -\n".encode()
        for i in range(start, start + count)
    )
    return (b"\xef\xbb\xbf" if bom else b"") + body


def _cfg(tmp_path, **overrides):
    values = {
        "browser_profile_dir": str(tmp_path / "wos-browser"),
        "browser_channel": "",
        "headless": True,
        "timeout_seconds": 60,
        "batch_size": 1000,
        "max_records_per_alert": 10000,
        "min_interval_seconds": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_browser(monkeypatch, page):
    sync = FakeSync(FakeContext(page))
    monkeypatch.setattr(wos_browser, "_load_sync_playwright", lambda: lambda: sync)
    return sync


ALERT_URL = ("https://www.webofscience.com/wos/woscc/"
             "alert-execution-summary/00000000-0000-0000-0000-"
             "000000000001")


def test_fetch_ris_batches_ranges_and_closes_context(tmp_path, monkeypatch):
    paths = []
    downloads = []
    for start, count in ((1, 1000), (1001, 201)):
        path = tmp_path / f"{start}.ris"
        path.write_bytes(_ris(start, count, bom=True))
        paths.append(path)
        downloads.append(FakeDownload(path))
    page = FakePage(1201, downloads)
    sync = _patch_browser(monkeypatch, page)

    result = wos_browser.fetch_ris(
        ALERT_URL,
        1201, _cfg(tmp_path))

    assert result.count(b"TY  - JOUR") == 1201
    assert b"\xef\xbb\xbf" not in result
    ranges = [a for a in page.actions if a[0] == "fill"]
    assert [(a[1], a[2]) for a in ranges] == [
        ("label=Input starting record range", "1"),
        ("label=Input ending record range. A maximum of 1000 records can be exported at one time.", "1000"),
        ("label=Input starting record range", "1001"),
        ("label=Input ending record range. A maximum of 1000 records can be exported at one time.", "1201"),
    ]
    assert sync.chromium.calls[0][1]["headless"] is True
    assert sync.chromium.calls[0][1]["timeout"] == 60000
    assert sync.chromium.calls[0][0] == str(Path(_cfg(tmp_path).browser_profile_dir))
    assert sync.chromium.context.closed is True
    assert sync.stopped is True
    assert page.dialog_open is False


def test_fetch_ris_rejects_total_mismatch_and_cleans_up(tmp_path, monkeypatch):
    page = FakePage(32, [])
    sync = _patch_browser(monkeypatch, page)

    with pytest.raises(wos_browser.WosFetchError, match="总数不匹配"):
        wos_browser.fetch_ris(
            ("https://webofscience.clarivate.cn/wos/woscc/"
             "alert-execution-summary/00000000-0000-0000-0000-"
             "000000000001"),
            31, _cfg(tmp_path))

    assert sync.chromium.context.closed is True
    assert sync.stopped is True


def test_fetch_ris_rejects_partial_download(tmp_path, monkeypatch):
    path = tmp_path / "partial.ris"
    path.write_bytes(_ris(1, 1))
    page = FakePage(3, [FakeDownload(path)])
    sync = _patch_browser(monkeypatch, page)

    with pytest.raises(wos_browser.WosFetchError, match="实际得到 1 条"):
        wos_browser.fetch_ris(ALERT_URL, 3, _cfg(tmp_path, batch_size=3))

    assert sync.chromium.context.closed is True
    assert sync.stopped is True


def test_fetch_ris_reports_download_failure_and_cleans_up(tmp_path, monkeypatch):
    class FailingPage(FakePage):
        def expect_download(self, **kwargs):
            raise RuntimeError("download failed")

    page = FailingPage(1, [])
    sync = _patch_browser(monkeypatch, page)

    with pytest.raises(wos_browser.WosFetchError, match="下载失败"):
        wos_browser.fetch_ris(ALERT_URL, 1, _cfg(tmp_path))

    assert sync.chromium.context.closed is True
    assert sync.stopped is True


def test_fetch_ris_retries_transport_failure_from_page_goto(
        tmp_path, monkeypatch):
    class NetworkFailurePage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            raise TimeoutError("network timeout")

    page = NetworkFailurePage(1, [])
    sync = _patch_browser(monkeypatch, page)

    with pytest.raises(wos_browser.WosFetchError, match="网络和机构访问") as exc:
        wos_browser.fetch_ris(ALERT_URL, 1, _cfg(tmp_path))

    assert not isinstance(exc.value, wos_browser.WosAccessError)
    assert sync.chromium.context.closed is True
    assert sync.stopped is True


def test_fetch_ris_keeps_non_wos_redirect_as_access_failure(
        tmp_path, monkeypatch):
    class RedirectPage(FakePage):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            self.url = "https://login.example.invalid/consent"

    page = RedirectPage(1, [])
    sync = _patch_browser(monkeypatch, page)

    with pytest.raises(wos_browser.WosAccessError, match="未允许的域名"):
        wos_browser.fetch_ris(ALERT_URL, 1, _cfg(tmp_path))

    assert sync.chromium.context.closed is True
    assert sync.stopped is True


def test_fetch_ris_retries_result_summary_wait_timeout(
        tmp_path, monkeypatch):
    class TimeoutSummaryPage(FakePage):
        def locator(self, selector, *, scope=None):
            locator = super().locator(selector, scope=scope)
            if selector == "#GenericFD-search-searchInfo-parent":
                def wait_for(**kwargs):
                    raise TimeoutError("summary timeout")

                locator.wait_for = wait_for
            return locator

    page = TimeoutSummaryPage(1, [])
    sync = _patch_browser(monkeypatch, page)

    with pytest.raises(wos_browser.WosFetchError, match="网络和机构访问") as exc:
        wos_browser.fetch_ris(ALERT_URL, 1, _cfg(tmp_path))

    assert not isinstance(exc.value, wos_browser.WosAccessError)
    assert sync.chromium.context.closed is True
    assert sync.stopped is True


def test_fetch_ris_reports_loaded_summary_without_total_as_access_failure(
        tmp_path, monkeypatch):
    page = FakePage(1, [], total_text="登录机构后才能查看结果")
    sync = _patch_browser(monkeypatch, page)

    with pytest.raises(wos_browser.WosAccessError, match="登录/授权"):
        wos_browser.fetch_ris(ALERT_URL, 1, _cfg(tmp_path))

    assert sync.chromium.context.closed is True
    assert sync.stopped is True


def test_fetch_ris_reports_missing_playwright_without_import(tmp_path, monkeypatch):
    monkeypatch.setattr(
        wos_browser, "_load_sync_playwright",
        lambda: (_ for _ in ()).throw(
            wos_browser.WosFetchError("需要 Playwright")))

    with pytest.raises(wos_browser.WosFetchError, match="需要 Playwright"):
        wos_browser.fetch_ris(ALERT_URL, 1, _cfg(tmp_path))


def test_fetch_ris_accepts_chinese_result_summary(tmp_path, monkeypatch):
    path = tmp_path / "one.ris"
    path.write_bytes(_ris(1, 1))
    page = FakePage(1, [FakeDownload(path)], total_text="共有 1 条提醒结果")
    sync = _patch_browser(monkeypatch, page)

    assert wos_browser.fetch_ris(ALERT_URL, 1, _cfg(tmp_path))
    assert sync.chromium.context.closed is True


def test_browser_login_is_headful_and_closes_dedicated_context(
        tmp_path, monkeypatch):
    page = FakePage(0, [])
    sync = _patch_browser(monkeypatch, page)
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    wos_browser.browser_login(_cfg(tmp_path, headless=True))

    kwargs = sync.chromium.calls[0][1]
    assert kwargs["headless"] is False
    assert kwargs["accept_downloads"] is True
    assert sync.chromium.context.closed is True
    assert sync.stopped is True


def test_validate_alert_url_canonicalises_safe_link_and_rejects_target_query():
    assert wos_browser.validate_alert_url(ALERT_URL + "/") == ALERT_URL
    wrapped = "https://safe.example/click?url=" + quote(ALERT_URL, safe="")
    assert wos_browser.validate_alert_url(wrapped) == ALERT_URL
    nested_bad = ("https://safe.example/click?url=" +
                  quote(ALERT_URL + "?page=1", safe=""))
    with pytest.raises(wos_browser.WosAccessError):
        wos_browser.validate_alert_url(nested_bad)


@pytest.mark.parametrize("url", [
    "http://www.webofscience.com/wos/woscc/alert",
    "https://evil.example/wos/woscc/alert",
    "https://www.webofscience.com/",
    "https://www.webofscience.com:invalid/wos/woscc/alert",
    "https://www.webofscience.com/wos/woscc/summary/alert",
    ALERT_URL + "?page=1",
])
def test_alert_url_is_restricted(url):
    with pytest.raises(wos_browser.WosAccessError):
        wos_browser.validate_alert_url(url)


# ------------------------------------------------------------- profile 目录边界
def test_browser_profile_directory_is_private(tmp_path):
    """profile 里是机构会话 cookie,必须是 0700(mkdir 的 mode 会被 umask 削掉)。"""
    import stat as stat_module

    profile = tmp_path / "wos-browser"

    class Chromium:
        def launch_persistent_context(self, path, **kwargs):
            return SimpleNamespace(path=path)

    wos_browser._launch_context(
        SimpleNamespace(chromium=Chromium()), _cfg(tmp_path), profile, 1000)

    assert stat_module.S_IMODE(profile.stat().st_mode) == 0o700


def test_profile_dir_pointing_at_a_daily_browser_is_rejected(tmp_path, monkeypatch):
    """只挡住主目录本身不够:~/.config/google-chrome/Default 曾照样放行。"""
    home = tmp_path / "home"
    (home / ".config" / "google-chrome" / "Default").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    candidates = [
        home / ".config" / "google-chrome",
        home / ".config" / "google-chrome" / "Default",
        home / ".config" / "chromium",
        home / ".mozilla",
    ]
    for bad in candidates:
        bad.mkdir(parents=True, exist_ok=True)
        with pytest.raises(wos_browser.WosFetchError, match="日常浏览器"):
            wos_browser._settings(_cfg(tmp_path, browser_profile_dir=str(bad)))


def test_existing_browser_profile_directory_is_rejected(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    existing = tmp_path / "daily"
    existing.mkdir()
    (existing / "Local State").write_text("{}", encoding="utf-8")

    with pytest.raises(wos_browser.WosFetchError, match="浏览器 profile"):
        wos_browser._settings(_cfg(tmp_path, browser_profile_dir=str(existing)))

    # 专用空目录照常可用
    dedicated = tmp_path / "dedicated"
    assert wos_browser._settings(
        _cfg(tmp_path, browser_profile_dir=str(dedicated)))[0] == dedicated


def test_dedicated_profile_is_reused_across_runs(tmp_path):
    """专用 profile 用过一次后也会有 Local State 等特征,不能被自己挡在门外。"""
    profile = tmp_path / "wos-browser"

    class Chromium:
        def launch_persistent_context(self, path, **kwargs):
            return SimpleNamespace()

    playwright = SimpleNamespace(chromium=Chromium())
    cfg = _cfg(tmp_path)

    wos_browser._launch_context(playwright, cfg, profile, 1000)
    # 模拟 Playwright 首次启动后在 profile 里留下的特征文件
    (profile / "Local State").write_text("{}", encoding="utf-8")
    (profile / "Default").mkdir()
    (profile / "Default" / "Cookies").write_text("x", encoding="utf-8")

    assert wos_browser._settings(cfg)[0] == profile
    wos_browser._launch_context(playwright, cfg, profile, 1000)
