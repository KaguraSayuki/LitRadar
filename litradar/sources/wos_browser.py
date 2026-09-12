"""从 Web of Science alert 页面导出 RIS。

WoS 的 alert 结果页通过浏览器导出完整题录。这个模块把浏览器交互限制在
一个专用 Playwright profile 中:调用者只给出已经验证过
的 WoS alert URL,模块负责打开结果页、按最多 1000 条一批导出 RIS,并在关闭
浏览器上下文前把下载内容读入内存。

Playwright 在函数调用时才导入,因此未安装浏览器依赖不会影响不使用 WoS 的
普通采集命令。
"""
from __future__ import annotations

import re
import time
import unicodedata
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlsplit

from .wos_email import canonical_wos_url

if TYPE_CHECKING:
    from ..config import WosConfig


class WosFetchError(RuntimeError):
    """WoS RIS 导出失败,调用者可以记录并在之后重试。"""


class WosAccessError(WosFetchError):
    """WoS 页面不可访问、未登录或机构授权失效。"""


ALLOWED_HOSTS = frozenset({"www.webofscience.com", "webofscience.clarivate.cn"})
HOME_URL = "https://www.webofscience.com/"
_ALERT_TOTAL_RE = re.compile(
    r"(?P<count>[0-9][0-9,]*)\s+alerting\s+results?\s+for\b", re.IGNORECASE)
_ALERT_TOTAL_LOCALISED_RE = re.compile(
    r"(?P<count>[0-9][0-9,]*)\s*(?:条|个)?\s*"
    r"(?:alerting\s*|提醒\s*)?(?:results?|结果)", re.IGNORECASE)
_ALERT_TOTAL_SUFFIX_RE = re.compile(
    r"(?:alerting\s+results?|结果)[^0-9]{0,20}"
    r"(?P<count>[0-9][0-9,]*)",
    re.IGNORECASE)
_RIS_RECORD_RE = re.compile(rb"(?m)^(?:TY)\s+-\s+\S")
_RIS_TITLE_RE = re.compile(rb"(?m)^(?:TI|T1)\s+-\s+\S")
_RIS_BOM = b"\xef\xbb\xbf"


def _is_final_wos_host(value: str) -> bool:
    try:
        return (urlsplit(value).hostname or "").lower().rstrip(".") in ALLOWED_HOSTS
    except ValueError:
        return False


def _final_target_has_query(value: str, *, depth: int = 0,
                            seen: frozenset[str] = frozenset()) -> bool:
    """Detect query/fragment on a final WoS target inside a safe link."""
    if depth > 6 or value in seen:
        return False
    seen = seen | {value}
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if _is_final_wos_host(value):
        return bool(parts.query or parts.fragment)
    try:
        query = parse_qs(parts.query, keep_blank_values=True)
    except ValueError:
        return False
    for key, candidates in query.items():
        if key.lower() not in {"url", "u"}:
            continue
        for candidate in candidates:
            decoded = candidate.strip(" \t\r\n\"'")
            for _ in range(6):
                parsed = urlsplit(decoded)
                if parsed.scheme and parsed.netloc:
                    break
                next_value = unquote(decoded).strip(" \t\r\n\"'")
                if next_value == decoded:
                    break
                decoded = next_value
            if _final_target_has_query(decoded, depth=depth + 1, seen=seen):
                return True
    return False


def validate_alert_url(alert_url: str) -> str:
    """验证并返回可导航的 WoS alert URL。

    只允许官方 WoS 两个地域域名和 HTTPS。这样邮件或数据库中的任意 URL
    都不会被当成浏览器导航目标。
    """
    if not isinstance(alert_url, str) or not alert_url.strip():
        raise WosAccessError("WoS alert URL 为空")
    value = alert_url.strip()
    try:
        if urlsplit(value).fragment:
            raise ValueError("WoS alert URL cannot have fragment")
        # A direct canonical WoS URL must not carry parameters which could
        # change the page reached after validation.  Safe-link wrappers may
        # still have their normal ``url``/``u`` query parameter; the shared
        # parser below unwraps that parameter and returns the canonical target.
        if _final_target_has_query(value):
            raise ValueError("WoS alert URL canonical target cannot have query")
        canonical = canonical_wos_url(value)
    except (TypeError, ValueError) as exc:
        raise WosAccessError(
            "WoS alert URL 必须是 HTTPS 的 /wos/<database>/"
            "alert-execution-summary/<UUID>，且 canonical URL 不得含 query") from exc
    return canonical


def _validate_allowed_page_url(page_url: str) -> None:
    """验证导航后的 URL 仍在允许的 WoS 域名内。"""
    if not page_url:
        return
    try:
        parts = urlsplit(page_url)
        port = parts.port
    except ValueError as exc:
        raise WosAccessError("WoS 页面 URL 格式无效") from exc
    host = (parts.hostname or "").lower().rstrip(".")
    if (parts.scheme.lower() != "https" or host not in ALLOWED_HOSTS
            or parts.username is not None or parts.password is not None
            or port not in (None, 443)):
        raise WosAccessError("WoS 页面跳转到了未允许的域名")


def _load_sync_playwright():
    """延迟加载 Playwright,并给出可操作的缺依赖提示。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 取决于部署环境
        raise WosFetchError(
            "WoS 浏览器功能需要 Playwright:请安装 playwright 并运行 "
            "`playwright install chromium`") from exc
    return sync_playwright


def _cfg_int(cfg: Any, name: str, default: int) -> int:
    value = getattr(cfg, name, default)
    if isinstance(value, bool):
        raise WosFetchError(f"WoS 配置 {name} 必须是整数")
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WosFetchError(f"WoS 配置 {name} 必须是整数") from exc
    return value


def _looks_like_browser_profile(path: Path) -> bool:
    """目录里已有 Chromium/Firefox 的 profile 特征文件即为日常浏览器目录。"""
    if (path / "Local State").exists() or (path / "prefs.js").exists():
        return True
    for child in ("Default", "Profile 1"):
        sub = path / child
        if (sub / "Cookies").exists() or (sub / "History").exists():
            return True
    return False


# 本工具自己创建的专用 profile 会留下这个标记。Playwright 首次启动后,专用
# 目录里同样会出现 Local State 等特征文件,所以只能靠标记区分"我们自己建的
# 专用 profile"和"用户的日常浏览器 profile"。
_PROFILE_MARKER = ".litradar-wos-profile"


def _is_foreign_browser_profile(path: Path) -> bool:
    return _looks_like_browser_profile(path) and not (path / _PROFILE_MARKER).exists()


def _forbidden_profile_roots(home: Path) -> tuple[Path, ...]:
    """日常浏览器的配置根目录 —— 自动化绝不能借用其中的会话。"""
    roots = [home / ".mozilla", home / "snap", home / ".var" / "app"]
    config = home / ".config"
    roots += [config / name for name in (
        "google-chrome", "chromium", "chromium-browser", "microsoft-edge",
        "BraveSoftware", "vivaldi",
    )]
    return tuple(roots)


def _settings(cfg: Any) -> tuple[Path, int, int, int, float]:
    profile_value = getattr(cfg, "browser_profile_dir", "./data/wos-browser")
    if not isinstance(profile_value, (str, Path)) or not str(profile_value).strip():
        raise WosFetchError("WoS browser_profile_dir 不能为空")
    profile = Path(profile_value).expanduser()
    try:
        profile_resolved = profile.resolve()
        home_resolved = Path.home().resolve()
    except OSError as exc:
        raise WosFetchError("WoS browser_profile_dir 路径无法解析") from exc
    if profile_resolved == home_resolved:
        raise WosFetchError("WoS 浏览器必须使用专用 profile,不能使用用户主目录")
    # 只挡住主目录本身是不够的:~/.config/google-chrome/Default 这类路径
    # 会直接把日常浏览器 profile 交给自动化,既可能外泄已登录会话,也可能
    # 被 Chromium 改写而损坏。因此再挡配置根目录和已有 profile 特征。
    for root in _forbidden_profile_roots(home_resolved):
        if profile_resolved == root or root in profile_resolved.parents:
            raise WosFetchError(
                "WoS browser_profile_dir 不能指向日常浏览器目录;请使用独立目录")
    if _is_foreign_browser_profile(profile_resolved):
        raise WosFetchError(
            "WoS browser_profile_dir 已是一个浏览器 profile;请改用独立空目录")

    timeout = _cfg_int(cfg, "timeout_seconds", 60)
    batch = _cfg_int(cfg, "batch_size", 1000)
    maximum = _cfg_int(cfg, "max_records_per_alert", 10000)
    try:
        interval = float(getattr(cfg, "min_interval_seconds", 2))
    except (TypeError, ValueError, OverflowError) as exc:
        raise WosFetchError("WoS 配置 min_interval_seconds 必须是数值") from exc
    if timeout <= 0:
        raise WosFetchError("WoS timeout_seconds 必须大于 0")
    if not 1 <= batch <= 1000:
        raise WosFetchError("WoS batch_size 必须在 1 到 1000 之间")
    if maximum <= 0:
        raise WosFetchError("WoS max_records_per_alert 必须大于 0")
    if interval < 0:
        raise WosFetchError("WoS min_interval_seconds 不能为负数")
    return profile, timeout * 1000, batch, maximum, interval


def _launch_context(
    playwright: Any,
    cfg: Any,
    profile: Path,
    timeout_ms: int,
    *,
    headless: bool | None = None,
) -> Any:
    """启动专用持久化上下文,不放宽 TLS 或浏览器安全策略。"""
    kwargs: dict[str, Any] = {
        "headless": (bool(getattr(cfg, "headless", True))
                     if headless is None else headless),
        "timeout": timeout_ms,
        "accept_downloads": True,
    }
    channel = getattr(cfg, "browser_channel", "")
    if channel:
        kwargs["channel"] = channel
    try:
        # profile 里是机构访问会话的 cookie,必须是私有目录(mkdir 的
        # mode 会被 umask 削掉,所以再 chmod 一次)。
        profile.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            profile.chmod(0o700)
        try:
            (profile / _PROFILE_MARKER).touch(exist_ok=True)
        except OSError:
            pass
        return playwright.chromium.launch_persistent_context(str(profile), **kwargs)
    except Exception as exc:  # noqa: BLE001  启动失败需转成可操作提示
        raise WosFetchError(
            "无法启动 WoS Chromium:请确认 Playwright/Chromium 已安装，且 "
            "browser_profile_dir 可写") from exc


def _page(context: Any) -> Any:
    pages = getattr(context, "pages", [])
    if pages:
        return pages[0]
    try:
        return context.new_page()
    except Exception as exc:  # noqa: BLE001
        raise WosFetchError("WoS 浏览器无法创建页面") from exc


def _safe_click(page: Any, selector: str, timeout_ms: int) -> bool:
    """尝试点击可选 Cookie 控件,不存在或已消失时忽略。"""
    try:
        locator = page.locator(selector)
        if hasattr(locator, "count") and locator.count() == 0:
            return False
        if hasattr(locator, "is_visible") and not locator.is_visible():
            return False
        locator.click(timeout=min(timeout_ms, 3000))
        return True
    except Exception:  # noqa: BLE001  Cookie banner 不是主流程
        return False


def _dismiss_cookie(page: Any, timeout_ms: int) -> None:
    # 优先拒绝可选项,然后关闭,最后才接受。不同地域的 banner 只出现其中一组。
    for selector in (
        "#onetrust-reject-all-handler",
        "#onetrust-close-btn-handler",
        "#onetrust-accept-btn-handler",
    ):
        if _safe_click(page, selector, timeout_ms):
            return
    # 某些页面只显示 Preference Center 入口;打开后再寻找拒绝/关闭控件。
    if _safe_click(page, "#onetrust-pc-btn-handler", timeout_ms):
        for selector in (
            "#onetrust-reject-all-handler",
            "#onetrust-close-btn-handler",
            "#onetrust-accept-btn-handler",
        ):
            if _safe_click(page, selector, timeout_ms):
                return


def _alert_total(page: Any, timeout_ms: int) -> int:
    try:
        locator = page.locator("#GenericFD-search-searchInfo-parent")
    except Exception as exc:  # noqa: BLE001
        raise WosFetchError(
            "WoS alert 结果摘要无法定位，请检查网络和机构访问") from exc
    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        # A missing/late summary is not evidence that the user needs to log
        # in.  In particular, Playwright timeouts also cover a page that is
        # still loading because of a transient network or institution proxy
        # failure; let the queue retry it as a fetch failure.
        raise WosFetchError(
            "WoS alert 结果页加载超时，请检查网络和机构访问") from exc
    try:
        text = locator.inner_text(timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        raise WosFetchError(
            "WoS alert 结果摘要读取失败，请检查网络和机构访问") from exc
    # The alert page is normally English, but an institution may persist a
    # Chinese locale in the dedicated browser profile.  The locator is still
    # the stable result-summary element; only its count wording changes.
    text = unicodedata.normalize("NFKC", (text or "")).replace("\xa0", " ")
    match = _ALERT_TOTAL_RE.search(text)
    if match is None:
        match = _ALERT_TOTAL_LOCALISED_RE.search(text)
    if match is None:
        match = _ALERT_TOTAL_SUFFIX_RE.search(text)
    if not match:
        raise WosAccessError(
            "WoS 结果页未找到 alert 总数，可能需要机构登录/授权；"
            f"请检查网络和机构访问(页面文本: {text[:160]!r})")
    return int(match.group("count").replace(",", ""))


def _dialog_scope(page: Any) -> Any:
    """Return the visible export dialog when Playwright exposes its role."""
    try:
        dialogs = page.get_by_role("dialog")
        if dialogs.count() > 0:
            return getattr(dialogs, "last", dialogs)
    except Exception:  # noqa: BLE001 - some WoS builds omit the dialog role
        pass
    return page


def _open_export_dialog(page: Any, timeout_ms: int) -> Any:
    try:
        page.locator("#export-trigger-btn").click(timeout=timeout_ms)
        page.locator("#exportToRisButton").click(timeout=timeout_ms)
        heading = page.get_by_role("heading", name="Export Records to RIS File")
        heading.wait_for(state="visible", timeout=timeout_ms)
        return _dialog_scope(page)
    except Exception as exc:  # noqa: BLE001
        raise WosFetchError("WoS 无法打开 RIS 导出窗口") from exc


def _prepare_export(page: Any, start: int, end: int, timeout_ms: int) -> None:
    dialog = _open_export_dialog(page, timeout_ms)
    try:
        radio = dialog.locator("#radio3-input")
        try:
            radio.check(timeout=timeout_ms)
        except AttributeError:
            radio.click(timeout=timeout_ms)
        start_input = dialog.get_by_label(
            "Input starting record range", exact=True)
        end_input = dialog.get_by_label(
            ("Input ending record range. A maximum of 1000 records can be "
             "exported at one time."),
            exact=True,
        )
        start_input.fill(str(start), timeout=timeout_ms)
        end_input.fill(str(end), timeout=timeout_ms)
        combo = dialog.get_by_role(
            "combobox", name=re.compile(r"^Filter by\b", re.IGNORECASE))
        combo.click(timeout=timeout_ms)
        page.locator("#option-authorTitleSourceAbstract").click(timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        raise WosFetchError(f"WoS RIS 导出范围 {start}-{end} 设置失败") from exc


def _download_bytes(download: Any) -> bytes:
    """在 browser context 关闭前读取下载内容。"""
    try:
        path = download.path()
    except Exception as exc:  # noqa: BLE001
        raise WosFetchError("WoS RIS 下载文件路径无法读取") from exc
    if not path:
        raise WosFetchError("WoS RIS 下载文件路径为空")
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise WosFetchError("WoS RIS 下载文件无法读取") from exc


def _without_bom(data: bytes) -> bytes:
    """Remove a per-download UTF-8 BOM before RIS chunks are concatenated."""
    return data[len(_RIS_BOM):] if data.startswith(_RIS_BOM) else data


def _ris_title_count(data: bytes) -> int:
    # RIS 文件可能带 UTF-8 BOM;只影响第一行的正则匹配。
    body = _without_bom(data)
    records = len(_RIS_RECORD_RE.findall(body))
    titles = len(_RIS_TITLE_RE.findall(body))
    if records == 0 or titles == 0 or records != titles:
        raise WosFetchError(
            f"WoS RIS 内容格式异常(记录 {records} 条,标题 {titles} 条)")
    return titles


def _export_batch(page: Any, start: int, end: int, timeout_ms: int) -> bytes:
    _prepare_export(page, start, end, timeout_ms)
    try:
        with page.expect_download(timeout=timeout_ms) as info:
            page.locator("#exportButton").click(timeout=timeout_ms)
        download = info.value
    except Exception as exc:  # noqa: BLE001
        raise WosFetchError(f"WoS RIS 导出 {start}-{end} 下载失败") from exc
    if download is None:
        raise WosFetchError(f"WoS RIS 导出 {start}-{end} 没有下载文件")
    data = _without_bom(_download_bytes(download))
    count = _ris_title_count(data)
    if count != end - start + 1:
        raise WosFetchError(
            f"WoS RIS 导出 {start}-{end} 实际得到 {count} 条,与请求范围不符")
    return data


def fetch_ris(alert_url: str, expected_total: int, cfg: "WosConfig") -> bytes:
    """打开一个 WoS alert 并返回完整 RIS 字节。

    ``expected_total`` 来自 alert 邮件/队列,同时会与结果页实际显示的总数
    交叉核对。任何少导、超导、登录失效或浏览器启动失败都会抛出异常,不会
    返回看似成功的部分结果。
    """
    url = validate_alert_url(alert_url)
    if isinstance(expected_total, bool) or not isinstance(expected_total, int):
        raise WosFetchError("WoS expected_total 必须是非负整数")
    if expected_total < 0:
        raise WosFetchError("WoS expected_total 必须是非负整数")
    profile, timeout_ms, batch_size, maximum, interval = _settings(cfg)
    if expected_total > maximum:
        raise WosFetchError(
            f"WoS alert 有 {expected_total} 条,超过 max_records_per_alert={maximum}")

    sync_playwright = _load_sync_playwright()
    context = None
    try:
        with sync_playwright() as playwright:
            context = _launch_context(playwright, cfg, profile, timeout_ms)
            try:
                if hasattr(context, "set_default_timeout"):
                    context.set_default_timeout(timeout_ms)
                page = _page(context)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception as exc:  # noqa: BLE001
                    raise WosFetchError(
                        "WoS alert 页面无法打开，请检查网络和机构访问") from exc
                # Keep an explicit off-site redirect as an access/security
                # failure.  It is distinct from a transport timeout above and
                # must not be retried as a transient fetch.
                _validate_allowed_page_url(getattr(page, "url", ""))
                _dismiss_cookie(page, timeout_ms)
                actual_total = _alert_total(page, timeout_ms)
                if actual_total != expected_total:
                    raise WosFetchError(
                        f"WoS alert 总数不匹配:邮件/队列 {expected_total} 条,"
                        f"结果页显示 {actual_total} 条")
                if expected_total == 0:
                    return b""

                chunks: list[bytes] = []
                last_export = 0.0
                for start in range(1, expected_total + 1, batch_size):
                    end = min(start + batch_size - 1, expected_total)
                    if chunks:
                        delay = interval - (time.monotonic() - last_export)
                        if delay > 0:
                            time.sleep(delay)
                    chunk = _export_batch(page, start, end, timeout_ms)
                    chunks.append(chunk)
                    last_export = time.monotonic()

                result = b"\n".join(chunks)
                if _ris_title_count(result) != expected_total:
                    raise WosFetchError(
                        f"WoS RIS 合并后标题数不匹配:期望 {expected_total} 条")
                return result
            finally:
                # 下载内容已在上面的 _download_bytes 中读入内存,此处可安全释放
                # 专用上下文;清理失败不应覆盖真正的抓取错误。
                if context is not None:
                    with suppress(Exception):
                        context.close()
    except WosFetchError:
        raise
    except Exception as exc:  # noqa: BLE001  sync_playwright/上下文异常
        raise WosFetchError(f"WoS 浏览器抓取失败: {exc}") from exc


def browser_login(cfg: "WosConfig") -> None:
    """在专用 profile 中打开 WoS,由用户手工完成机构登录。

    该函数只应由显式的登录命令调用。它不填写凭据、不绕过验证码、也不
    操作当前用户的其他浏览器 profile;用户按 Enter 后才关闭上下文以保存
    Playwright persistent profile 的会话数据。
    """
    profile, timeout_ms, _, _, _ = _settings(cfg)
    sync_playwright = _load_sync_playwright()
    context = None
    try:
        with sync_playwright() as playwright:
            context = _launch_context(
                playwright, cfg, profile, timeout_ms, headless=False)
            try:
                if hasattr(context, "set_default_timeout"):
                    context.set_default_timeout(timeout_ms)
                page = _page(context)
                try:
                    # The only programmatic login navigation is this fixed
                    # official home page.  After it opens, the user may need
                    # to follow an institution SSO page manually; that flow
                    # is intentionally not treated as an alert redirect.
                    page.goto(HOME_URL, wait_until="domcontentloaded",
                              timeout=timeout_ms)
                except Exception as exc:  # noqa: BLE001
                    raise WosFetchError(
                        "WoS 登录页面无法打开，请检查网络和机构访问") from exc
                try:
                    input("请在打开的 WoS 窗口完成机构登录，完成后按 Enter 保存并退出：")
                except (EOFError, KeyboardInterrupt) as exc:
                    raise WosFetchError("WoS 登录未收到完成确认，请重新运行 wos-login") from exc
            finally:
                if context is not None:
                    with suppress(Exception):
                        context.close()
    except WosFetchError:
        raise
    except Exception as exc:  # noqa: BLE001  启动/交互异常需给队列可读信息
        raise WosFetchError(f"WoS 登录浏览器失败: {exc}") from exc


__all__ = [
    "ALLOWED_HOSTS", "WosAccessError", "WosFetchError", "browser_login",
    "fetch_ris", "validate_alert_url",
]
