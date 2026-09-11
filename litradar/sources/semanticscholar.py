"""Semantic Scholar Graph API —— 摘要主力来源。

为什么需要它:Crossref 里 ACS 系期刊(JACS / Org. Lett. / JOC)常常没有摘要,
而 Semantic Scholar 实测 4/4 都能补上,且免费。

限流:无 API Key 时是共享池,约 1 req/s 且可能 429。建议免费申请一个 Key
(https://www.semanticscholar.org/product/api#api-key-form),配到 S2_API_KEY。
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

BASE = "https://api.semanticscholar.org/graph/v1"
FIELDS = "title,abstract,tldr,citationCount,venue,year,publicationDate,externalIds,openAccessPdf"

# ── 限流 ────────────────────────────────────────────────────────────────────
# 官方规定:**1 请求/秒,跨所有端点累加**。
#
# ⚠️ 一个我踩过的坑:API Key 给的是**独立的** 1 req/s(不再与匿名用户抢共享池),
#    **不是更高的速率**。我一开始误以为"有 key 就能快 4 倍",把间隔设成 0.25s,
#    实际是 4 req/s —— 超限 4 倍,会被拒。
#    现在有无 key 都守同一个间隔。
#
# 默认取 5s(0.2 req/s),比官方的 1 req/s 宽松 5 倍 —— 反正 batch 端点
# 一次能吃 100 个 DOI,169 篇文献也只要 2 次请求,放慢几乎不增加总耗时,
# 却基本排除被限流的可能。可用 config.yaml 的 sources.s2_min_interval 调整。
MIN_INTERVAL = float(os.environ.get("S2_MIN_INTERVAL", "5.0"))

_last = [0.0]

# 运行期状态:key 被拒后自动降级为匿名访问。
# 动机:实测「无效 key」会让所有请求 403,而匿名访问反而是 200 ——
# 也就是说配错 key 比不配更糟。这里检测到就自动摘掉,保证富化仍能进行。
_key_state = {"rejected": False, "warned": False}


def _headers() -> dict[str, str]:
    h = {"User-Agent": "LitRadar/0.1 (personal literature radar)"}
    key = os.environ.get("S2_API_KEY")
    if key and not _key_state["rejected"]:
        h["x-api-key"] = key
    return h


def _note_rejected() -> None:
    """带 key 却拿到 403 —— 判定 key 当前不可用,后续改走匿名。

    注意官方状态码语义:401 = 凭据无效;403 = 请求被理解但无权限。
    实测拿到的是 **403**,更可能是 key 审核/激活尚未完成,而非填错。
    """
    if not os.environ.get("S2_API_KEY") or _key_state["rejected"]:
        return
    _key_state["rejected"] = True
    if not _key_state["warned"]:
        _key_state["warned"] = True
        print("    [warn] S2 返回 403(无权限),已自动降级为匿名访问。"
              "官方语义下 403 通常表示 key 尚未激活/审核未完成,而非填错;"
              "匿名共享池限流更紧,结果可能不全。")



def _throttle(min_interval: float = MIN_INTERVAL) -> None:
    wait = min_interval - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()


def fetch_by_doi(doi: str, retries: int = 3, interval: float | None = None) -> dict | None:
    """按 DOI 取单篇。"""
    interval = MIN_INTERVAL if interval is None else interval
    for attempt in range(retries):
        _throttle(interval)
        try:
            r = requests.get(
                f"{BASE}/paper/DOI:{doi}",
                params={"fields": FIELDS},
                headers=_headers(),
                timeout=30,
            )
            if r.status_code == 404:
                return None
            if r.status_code == 403 and os.environ.get("S2_API_KEY") \
                    and not _key_state["rejected"]:
                _note_rejected()
                continue                      # 立刻用匿名头重试
            if r.status_code in (429, 403):
                time.sleep(3.0 * (attempt + 1))
                continue
            r.raise_for_status()
            return _to_dict(r.json(), doi)
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (attempt + 1))
    return None


# 批量端点一次最多 500 个 ID;取 100 留足余量,也避免单次响应过大
BATCH_SIZE = 100


def fetch_many_by_doi(dois: list[str], retries: int = 3,
                      verbose: bool = False,
                      interval: float | None = None) -> dict[str, dict]:
    """批量按 DOI 取。返回 ``{doi: record}``,取不到的 DOI 不出现在结果里。

    用 ``POST /graph/v1/paper/batch``:一次请求最多 500 篇,实测 6 篇 0.3 秒,
    而逐条请求 5 篇要 7.1 秒。这是把请求量压下来、遵守 1 req/s 限制的关键。

    ⚠️ 失败会打警告而不是静默返回空 —— 否则限流或网络问题时表现为
    "全都补不到摘要",很难排查(实测踩过这个坑)。
    """
    out: dict[str, dict] = {}
    ids = [d for d in dict.fromkeys(dois) if d]      # 去重保序
    if not ids:
        return out

    interval = MIN_INTERVAL if interval is None else interval
    for start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[start:start + BATCH_SIZE]
        payload = {"ids": [f"DOI:{d}" for d in chunk]}
        last_err = ""
        for attempt in range(retries):
            _throttle(interval)
            try:
                r = requests.post(
                    f"{BASE}/paper/batch",
                    params={"fields": FIELDS},
                    json=payload,
                    headers=_headers(),
                    timeout=60,
                )
                # 带 key 却 403 = key 不可用 -> 摘掉 key 立即重试
                if r.status_code == 403 and os.environ.get("S2_API_KEY") \
                        and not _key_state["rejected"]:
                    _note_rejected()
                    continue
                # 429 = 限流;403 = 未认证共享池耗尽时的另一种表现。
                # 两者都重试,退避要够长 —— 共享池是全局争抢的,3 秒往往不够。
                if r.status_code in (429, 403):
                    last_err = f"HTTP {r.status_code} 限流/共享池耗尽"
                    time.sleep(5.0 * (attempt + 1) ** 1.5)
                    continue
                if r.status_code in (400, 404):
                    last_err = f"HTTP {r.status_code}: {r.text[:120]}"
                    break
                r.raise_for_status()
                got = 0
                for doi, rec in zip(chunk, r.json()):
                    if rec:
                        out[doi] = _to_dict(rec, doi)
                        got += 1
                if verbose and got < len(chunk):
                    print(f"    [S2] 批量 {start + 1}-{start + len(chunk)}: "
                          f"{got}/{len(chunk)} 命中")
                last_err = ""
                break
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                time.sleep(1.5 * (attempt + 1))
        if last_err:
            print(f"    [warn] S2 批量 {start + 1}-{start + len(chunk)} 失败: "
                  f"{last_err[:140]}")
    return out


def _to_dict(p: dict, doi: str | None = None) -> dict:
    oa = p.get("openAccessPdf") or {}
    return {
        # ⚠️ 必须带 kind。`_to_dict` 同时服务于「富化」和「检索」两条路径,
        # 入库时 item.kind 是 NOT NULL —— 漏了会让整批插入失败:
        #   NOT NULL constraint failed: item.kind
        "kind": "paper",
        "title": p.get("title"),
        "abstract": p.get("abstract"),
        "tldr": (p.get("tldr") or {}).get("text"),
        "cited_by_count": p.get("citationCount"),
        "journal": p.get("venue"),
        "published_at": p.get("publicationDate") or (
            f"{p['year']}-01-01" if p.get("year") else None),
        "doi": (p.get("externalIds") or {}).get("DOI") or doi,
        "oa_url": oa.get("url"),
        "source": "semanticscholar",
    }


def search(query: str, *, limit: int = 20, year: str | None = None) -> list[dict]:
    """相关性检索(/paper/search)。最多 100 条、最多 1000 条结果,不支持查询语法。

    仅作兜底;正式召回请用 search_bulk() —— 那个支持语法且单次可拉 1000 篇。
    """
    if not os.environ.get("S2_API_KEY"):
        return []
    params: dict[str, Any] = {"query": query, "limit": min(limit, 100), "fields": FIELDS}
    if year:
        params["year"] = year
    try:
        _throttle(MIN_INTERVAL)      # 和其他端点共用同一个 1 req/s 额度
        r = requests.get(f"{BASE}/paper/search", params=params,
                         headers=_headers(), timeout=40)
        if r.status_code != 200:
            return []
        return [_to_dict(p) for p in r.json().get("data", [])]
    except Exception:  # noqa: BLE001
        return []


# bulk 端点单页最多 1000 条(官方文档)
BULK_PAGE = 1000

# ⚠️ bulk 端点**不支持 tldr** —— 共用 FIELDS 会直接 400:
#     {"error":"Unrecognized or unsupported fields: [tldr]"}
# 实测 title,tldr -> 400;下面这套 -> 200。所以单独一份。
BULK_FIELDS = ("title,abstract,venue,year,publicationDate,externalIds,"
               "citationCount,openAccessPdf,authors,publicationTypes,url")


def search_bulk(query: str, *, year: str | None = None, since: str | None = None,
                venues: list[str] | None = None,
                sort: str = "publicationDate:desc",
                max_pages: int = 1, retries: int = 3, verbose: bool = False,
                interval: float | None = None) -> list[dict]:
    """批量检索(/paper/search/bulk)。单页最多 1000 条,支持查询语法与排序。

    ⚠️ **必须用查询语法**,否则静默返回 0 条。实测:
        '"N-H insertion" + diazo + aniline'        -> 命中 3
        'diazo carbene N-H insertion aniline'      -> 命中 0   ← 裸词被当短语
    语法:``+`` = AND,``|`` = OR,``"..."`` = 短语,``-`` = 排除。

    与 Crossref 的分工:Crossref 模糊匹配、召回高但噪声大;bulk 是精确 AND、
    召回低但准确率高。两者互补,不是替换关系。
    """
    if not os.environ.get("S2_API_KEY"):
        return []
    interval = MIN_INTERVAL if interval is None else interval
    out: list[dict] = []
    token: str | None = None

    for page in range(max(1, max_pages)):
        params: dict[str, Any] = {"query": query, "fields": BULK_FIELDS, "sort": sort}
        if year:
            params["year"] = year
        if venues:
            # 实测:多刊必须用**逗号**分隔;用 | 会返回 0 条。
            params["venue"] = ",".join(venues)
        if token:
            params["token"] = token
        r = None
        last = ""
        for attempt in range(max(1, retries)):
            _throttle(interval)
            try:
                r = requests.get(f"{BASE}/paper/search/bulk", params=params,
                                 headers=_headers(), timeout=60)
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {str(e)[:70]}"
                r = None
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code in (429, 403):
                # 实测:5s 后仍 429,15s 后成功。响应里**没有** Retry-After,
                # 所以自己退避,别直接放弃(之前的实现就是直接放弃,导致静默少数据)。
                wait = float(r.headers.get("Retry-After") or 0) or (8 * (attempt + 1))
                last = f"HTTP {r.status_code} 限流"
                if verbose:
                    print(f"    [S2] 限流,等 {wait:.0f}s 重试({attempt + 1}/{retries})")
                time.sleep(wait)
                continue
            break

        if r is None or r.status_code != 200:
            print(f"    [warn] S2 bulk 放弃({last or '未知'}): query={query[:44]!r}")
            break

        j = r.json()
        data = j.get("data") or []
        if verbose:
            print(f"    [S2] {query[:44]!r} 第 {page + 1} 页:命中 {j.get('total')},"
                  f"本页 {len(data)}")
        for p in data:
            # year 只有年粒度(传 "2025-2026" 会把 2025 年 1 月的也拉回来,
            # 那可能是一年半以前)。这里按精确日期再过一道,和排序窗口对齐。
            if since:
                d = p.get("publicationDate") or ""
                if d and d < since:
                    continue
            out.append(_to_dict(p))
        token = j.get("token")
        if not token or not data:
            break
    return out
