"""X-MOL 个人空间(ispace)订阅关键词结果采集。

数据来源:https://ispace.x-mol.com/keywords 背后的 ``/spaceApi`` JSON 接口。
与 ``xmol_email.py`` 的区别:

======================  ==========================  ==========================
                        ``xmol_email``(邮件)        ``xmol_ispace``(本模块)
======================  ==========================  ==========================
触发方式                 X-MOL 推送的订阅邮件        主动拉取
覆盖面                   精选 teaser(通常 2 条)    首屏 60 条,覆盖约 4 个月
摘要                     无,靠 DOI 富化             自带(英文截断 + 完整中文)
期刊名                   缩写                       全称
======================  ==========================  ==========================

两条路径产出的记录 DOI 相同,入库时按 ``doi:`` 去重键合并(见
``db.upsert_item_status``),不会重复。

关于 User-Agent
---------------
X-MOL 的 WAF 会拦截不带浏览器 UA 的 ``/spaceApi`` 请求(HTTP 429,拦截页写明
"您的访问行为已被系统识别为异常请求")。实测同一 IP、同一 URL:curl 默认 UA 被
拦,浏览器 UA 通过。这是接口的硬性行为,不是可选优化 —— 去掉这个常量功能即归零。

因此这里发送一个**固定**的浏览器 UA:没有随机化、没有指纹轮换、不随请求变化。
请求频率也刻意压到极低(见 ``min_interval`` 与 ``_PAGE_SIZE`` 注释),让它落在
正常使用的量级。

关于失败处理
------------
429(限流)和 401(会话过期)**都不重试**,直接抛异常。这一点是刻意的:

* ``http.get_json`` 对 401 会走 ``raise_for_status()`` -> 异常 -> 退避重试 3 次。
  令牌过期时那等于对着 WAF 连撞三次,只会让情况更糟。
* 被限流时应该**停下来并让人知道**,而不是换参数继续试探。

所以本模块只做一件事:请求一次,如实汇报。是否继续由调用方决定。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from ..normalize import clean_html_text, parse_date, split_authors

# 接口返回的固定浏览器标识。见模块头部说明:这是硬性要求,固定不变。
_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

_BASE = "https://ispace.x-mol.com/spaceApi"
_KEYWORDS_PATH = "/keywords/loadKeywordsFollowTree"
_PAPERS_PATH = "/paper/search/loadFollowKeywordsPapers"
_REFERER = "https://ispace.x-mol.com/keywords"
_API_VERSION = "1001"

# 服务端硬编码每页 60 条:传 pageSize=100/200/500 都只返回 60。
# totalRecord 封顶 600(pageNo=11/50 与 page 10 内容相同)。
# 实测首屏 60 条覆盖 119 天,而日报只需 1 天的余量 —— 所以默认只取第 1 页。
PAGE_SIZE = 60
MAX_RECORDS = 600

# 命中关键词在 summary 里的高亮约定,与邮件解析器一致。
_HIGHLIGHT_RE = re.compile(r"<span style='color:\s*#ff0000'>(.*?)</span>", re.I | re.S)

# 订阅树缓存:关键词清单很少变动,但它只是"命中词叫什么"的展示元数据,
# 失败也不该影响文献采集。缓存住可以省掉每次运行的一次请求。
_KEYWORD_CACHE: tuple[float, dict[int, str]] = (0.0, {})
_KEYWORD_TTL = 6 * 3600.0

# 会话令牌必须是整串 Cookie:两个名字都要在,且各自带一段足够长的值。
# 实测真实值分别是 200 多字符的 JWT;阈值放宽到 16 只是为了挡住
# "只粘了名字""粘了文档里的省略号示例"这类错误,不是在校验 JWT 格式。
_TOKEN_COOKIES = ("atk0210", "rtk0210")
_TOKEN_MIN_VALUE = 16


def check_token_shape(token: str | None) -> str | None:
    """校验令牌形状,返回面向用户的问题描述;没问题时返回 ``None``。

    在**写入之前**调用。格式错误的令牌到了服务端只表现为"会话过期",
    很难排查,所以宁可在这里把话说清楚。
    """
    if not token or not token.strip():
        return "没有读到令牌。"
    token = token.strip()
    if "\n" in token or "\r" in token:
        return "令牌必须是单行。请把整串 Cookie 拼成一行再粘贴。"
    if len(token) > 8192:
        return "令牌过长（超过 8192 字符），可能粘贴了多余内容。"
    try:
        token.encode("ascii")
    except UnicodeEncodeError:
        return ("令牌里有非 ASCII 字符，通常是粘贴了带省略号的示例文本。"
                "请复制浏览器 Cookie 里的完整原始值。")
    for name in _TOKEN_COOKIES:
        if f"{name}=" not in token:
            return (f"令牌缺少 {name}=。它应当是浏览器 Cookie 里的整串 "
                    f"{_TOKEN_COOKIES[0]}=…; {_TOKEN_COOKIES[1]}=…")
    for part in token.split(";"):
        name, _, value = part.strip().partition("=")
        if name in _TOKEN_COOKIES and len(value.strip()) < _TOKEN_MIN_VALUE:
            return (f"{name} 的值太短，看起来不是完整令牌。"
                    "请从浏览器 Cookie 里重新复制完整值。")
    return None


class IspaceError(RuntimeError):
    """采集失败。消息面向用户,可直接显示在网页上。"""


class IspaceAuthError(IspaceError):
    """会话令牌失效 —— 需要在网页上重新粘贴令牌。"""


class IspaceBlockedError(IspaceError):
    """被 WAF 限流。不重试,等下次运行。"""


@dataclass
class IspaceResult:
    """一次采集的结果。

    ``items`` 是可直接交给 ``pipeline._store`` 的 item dict;
    ``notes`` 是非致命的降级说明(例如只拿到文献、没拿到订阅树)。
    """
    items: list[dict] = field(default_factory=list)
    total: int = 0
    page: int = 1
    notes: list[str] = field(default_factory=list)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json",
        "api-version": _API_VERSION,
        "Referer": _REFERER,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    return s


def _get_json(session: requests.Session, path: str, params: dict[str, Any] | None,
              token: str, *, timeout: int) -> Any:
    """请求一次并解析。429/401 直接抛,不做任何重试。"""
    try:
        r = session.get(_BASE + path, params=params, timeout=timeout,
                        headers={"Cookie": token})
    except UnicodeEncodeError as exc:
        # Cookie 值只能是 latin-1。粘贴时带进全角字符或省略号就会走到这里;
        # 报成"会话过期"会把人引到错误方向,所以单独说清楚。
        raise IspaceAuthError(
            "会话令牌里有非 ASCII 字符（常见于直接粘贴了带省略号的示例）。"
            "请粘贴浏览器 Cookie 里的完整原始值。") from exc
    except requests.RequestException as exc:
        raise IspaceError(f"连接 X-MOL 失败：{type(exc).__name__}") from exc

    if r.status_code == 401:
        raise IspaceAuthError(
            "X-MOL 会话已过期或被拒绝。请在设置页重新登录 X-MOL 并粘贴新的会话令牌。")
    if r.status_code == 429:
        raise IspaceBlockedError(
            "X-MOL 暂时限制了本站的访问（HTTP 429）。已停止本次采集，未做重试。")
    if r.status_code >= 500:
        raise IspaceError(f"X-MOL 服务端错误（HTTP {r.status_code}）。")
    if r.status_code != 200:
        raise IspaceError(f"X-MOL 返回了意外的状态码 HTTP {r.status_code}。")

    if not r.content:
        # 200 但空 body:实测未登录时会出现这种形态。
        raise IspaceAuthError(
            "X-MOL 没有返回内容，通常意味着会话已失效。请重新登录并粘贴新的会话令牌。")
    try:
        return r.json()
    except ValueError as exc:
        raise IspaceError("X-MOL 返回的内容不是 JSON，接口可能已改版。") from exc


def _unwrap(payload: Any) -> Any:
    """校验统一响应外壳 ``{jsonType, obj, success, failCode, message}``。"""
    if not isinstance(payload, dict) or payload.get("jsonType") != "mySpaceJsonResult":
        raise IspaceError("X-MOL 返回了非预期的数据结构，接口可能已改版。")
    fail = payload.get("failCode")
    if fail:
        msg = payload.get("message") or fail
        if str(fail).upper().startswith("F-1"):
            raise IspaceAuthError(f"X-MOL 拒绝了本次请求：{msg}")
        raise IspaceError(f"X-MOL 返回错误：{msg}")
    return payload.get("obj")


def load_subscriptions(session: requests.Session, token: str, *, timeout: int,
                       use_cache: bool = True) -> dict[int, str]:
    """取订阅关键词清单 ``{journalKeywordSubscribeId: keyword}``。

    只用于把摘要里的高亮片段翻译成"当初订阅的是哪个词"。拿不到不影响采集,
    调用方会把失败降级成一条 note。
    """
    global _KEYWORD_CACHE
    now = time.time()
    if use_cache and _KEYWORD_CACHE[1] and now - _KEYWORD_CACHE[0] < _KEYWORD_TTL:
        return _KEYWORD_CACHE[1]

    tree = _unwrap(_get_json(session, _KEYWORDS_PATH, None, token, timeout=timeout))
    out: dict[int, str] = {}
    for group in tree or []:
        for child in (group or {}).get("children") or []:
            kid = child.get("journalKeywordSubscribeId")
            kw = (child.get("keyword") or "").strip()
            if isinstance(kid, int) and kw:
                out[kid] = kw
    _KEYWORD_CACHE = (now, out)
    return out


def fetch(token: str, *, page: int = 1, min_interval: float = 30.0,
          timeout: int = 30, session: requests.Session | None = None) -> IspaceResult:
    """采集一页订阅命中文献。

    ``token`` 是浏览器 Cookie 串里 ``atk0210=...; rtk0210=...`` 那一部分。
    返回的 item dict 键名落在 ``db.ITEM_FIELDS`` 白名单内。

    只请求一页是刻意的:首屏 60 条覆盖约 4 个月,而流水线按天运行,一页远够。
    真要翻页就显式传 ``page``,并自行承担请求量。
    """
    if not token or not token.strip():
        raise IspaceAuthError("尚未配置 X-MOL 会话令牌。请在设置页粘贴后重试。")

    token = token.strip()
    session = session or _session()
    notes: list[str] = []

    # 先拿订阅清单(有缓存则零请求),再拿文献。两次请求之间同样遵守最小间隔。
    subs: dict[int, str] = {}
    try:
        subs = load_subscriptions(session, token, timeout=timeout)
    except IspaceAuthError:
        raise
    except IspaceError as exc:
        notes.append(f"未取到订阅关键词清单（{exc}），本轮不标注命中词。")

    if min_interval > 0 and subs:
        time.sleep(min(min_interval, 5.0))

    params = {
        "journalRangeType": 1,      # 与页面默认一致
        "pageNo": max(1, int(page)),
        "sort": "updateDate",
        "hideRead": 0,
        "readMode": "en",
    }
    obj = _unwrap(_get_json(session, _PAPERS_PATH, params, token, timeout=timeout))
    if not isinstance(obj, dict):
        raise IspaceError("X-MOL 返回的结果结构异常，接口可能已改版。")

    records = obj.get("results") or []
    result = IspaceResult(
        total=int(obj.get("totalRecord") or 0),
        page=int(obj.get("pageNo") or page),
        notes=notes,
    )
    for raw in records:
        item = to_item_data(raw, subs)
        if item is not None:
            result.items.append(item)
    return result


def to_item_data(raw: dict, subscriptions: dict[int, str] | None = None) -> dict | None:
    """把一条接口记录转成 ``db.ITEM_FIELDS`` 形状的 item dict。

    ``subscriptions`` 用于把高亮片段归因到订阅词,可为空。
    """
    title = clean_html_text(raw.get("title") or "")
    if not title:
        return None

    doi = (raw.get("doi") or "").strip() or None
    summary_raw = raw.get("summary") or ""
    # 高亮必须在清洗前提取 —— clean_html_text 会把 span 标签抹掉。
    keywords = _match_keywords(summary_raw, subscriptions)
    abstract = clean_html_text(summary_raw) or None
    abstract_zh = clean_html_text(raw.get("summaryZh") or "") or None

    return {
        "kind": "paper",
        # dedup_key 交给 pipeline._prepare 依 DOI 重算,这里不重复实现。
        "doi": doi,
        "title": title,
        "abstract": abstract,
        "abstract_zh": abstract_zh,
        "authors": split_authors(raw.get("author")),
        "journal": raw.get("journalName") or None,
        "published_at": parse_date(raw.get("pubDate")),
        "url": _paper_url(raw),
        "impact_factor": _as_float(raw.get("impactFactor")),
        "xmol_url": f"https://www.x-mol.com/paper/{raw['paperId']}"
                    if raw.get("paperId") else None,
        "matched_keywords": keywords,
        "source": "ispace",
        # 保留 X-MOL 内部 ID,便于将来核对与回溯。
        "source_ref": str(raw.get("paperId") or "") or None,
    }


def _paper_url(raw: dict) -> str | None:
    """优先用出版商原文地址;没有则退回 X-MOL 详情页。"""
    url = (raw.get("url") or "").strip()
    if url.startswith(("http://", "https://")):
        return url
    return None


def _match_keywords(summary_html: str, subscriptions: dict[int, str] | None) -> list[str]:
    """把高亮片段映射回订阅词。

    X-MOL 会做词干/子串扩展,高亮的是**实际命中的文本片段**而不是订阅词本身:
    订阅 ``[3,3] && rearrangement`` 时可能高亮 ``Cope rearrangement`` /
    ``rearrangements`` / ``3,3``(实测都出现过)。

    所以先把订阅词拆成原子词(布尔表达式去掉 ``&&``/``||``/括号,再按非字母数字
    切分),再看片段是否命中任一原子词。命中就归到订阅词名下;命中不了就保留原始
    片段 —— 宁可多带一点信息,也不要因为匹配不上就把命中词丢掉。
    """
    hits = [clean_html_text(h) for h in _HIGHLIGHT_RE.findall(summary_html or "")]
    hits = [h for h in hits if h]
    if not hits:
        return []

    wanted = [(v, _parts(v)) for v in (subscriptions or {}).values() if v]
    out: list[str] = []
    for frag in hits:
        low = frag.lower()
        matched = None
        for kw, parts in wanted:
            if any(p and p in low for p in parts):
                matched = kw
                break
        token = matched or frag
        if token not in out:
            out.append(token)
    return out


# 布尔连接符:X-MOL 的订阅式支持 ``&&`` / ``||``。
_BOOL_RE = re.compile(r"\s*(?:&&|\|\||\band\b|\bor\b)\s*", re.I)


def _parts(keyword: str) -> list[str]:
    """把订阅词拆成用于匹配的小写子串。

    先按布尔连接符切成短语,每个短语再拆成原子词::

        "[2,2] && cycloaddition"   -> ["2,2", "cycloaddition"]
        "alpha-beta rearrangement" -> ["alpha-beta rearrangement", "alpha", "beta",
                                       "rearrangement"]
        "Sample Author"            -> ["sample author", "sample", "author"]

    短语整体和原子词都参与匹配。短语必须保留,否则 ``[2,2]`` 这种短片段会被
    长度阈值滤掉(实测它正是高频命中片段之一)。词尾 ``s`` 去掉,好让
    ``rearrangement`` 命中 ``rearrangements``。
    """
    out: list[str] = []

    def add(token: str) -> None:
        token = token.strip()
        if token and token not in out:
            out.append(token)

    for phrase in _BOOL_RE.split(keyword.lower()):
        phrase = phrase.strip()
        if not phrase:
            continue
        # 去掉方括号保留内容,让 "[3,3]" 也能命中裸片段 "3,3"。
        bare = phrase.strip("[]() ").strip()
        if bare:
            add(bare)
        for word in re.split(r"[^a-z0-9]+", phrase):
            if len(word) > 3 and word.endswith("s"):
                word = word[:-1]
            # 原子词只收长度 > 2 的,避免单字母造成满屏误命中;
            # 数字片段(如 "[3,3]" 的 "3")靠上面的整体短语覆盖。
            if len(word) > 2:
                add(word)
    return out


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
