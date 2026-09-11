"""Crossref 关键词检索与单篇富化。

Crossref 的价值:元数据最权威(期刊全称、卷期页、ISSN、作者机构),
但 ACS 系期刊常常不提供摘要 —— 摘要交给 OpenAlex 补。
"""
from __future__ import annotations

import re
from typing import Any

from ..http import get_json
from ..normalize import days_ago, parse_date

BASE = "https://api.crossref.org"
_TAG_RE = re.compile(r"<[^>]+>")


def clean_abstract(raw: str | None) -> str | None:
    """Crossref 摘要是 JATS XML,去掉标签。"""
    if not raw:
        return None
    txt = _TAG_RE.sub(" ", raw)
    txt = re.sub(r"\s+", " ", txt).strip()
    if txt.lower().startswith("abstract"):
        txt = txt[8:].lstrip(" :")
    return txt or None


def _date_from_parts(parts: list | None) -> str | None:
    """把 Crossref 的 date-parts 转成 ISO 日期。

    月份缺失时返回 None —— 交给上层继续找更精确的日期,
    **绝不擅自补成 1 月**,否则新论文会被时间窗误伤。
    """
    if not parts or not parts[0]:
        return None
    y = int(parts[0])
    mo = int(parts[1]) if len(parts) > 1 and parts[1] else None
    if mo is None:
        return None
    d = int(parts[2]) if len(parts) > 2 and parts[2] else 1
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _best_date(m: dict) -> str | None:
    """挑最可信的发表日期。

    Crossref 里不少条目的 ``issued`` **只有年份**,若直接补成 1 月 1 日,
    这些"其实很新"的论文会被时间窗误伤(实测 25/101 条踩到这个坑)。
    优先级:issued > published-online > published-print > created(入库时间)。
    """
    for key in ("issued", "published-online", "published-print"):
        block = m.get(key)
        if not isinstance(block, dict):
            continue
        parts = (block.get("date-parts") or [None])[0]
        got = _date_from_parts(parts)
        if got:
            return got

    created = (m.get("created") or {}).get("date-time") or ""
    mm = re.match(r"(\d{4})-(\d{2})-(\d{2})", created)
    if mm:
        return f"{mm.group(1)}-{mm.group(2)}-{mm.group(3)}"

    parts = ((m.get("issued") or {}).get("date-parts") or [[None]])[0]
    if parts and parts[0]:
        return f"{int(parts[0]):04d}-01-01"
    return None


def _msg_to_item(m: dict, source: str = "crossref") -> dict | None:
    title = (m.get("title") or [None])[0]
    if not title:
        return None
    doi = (m.get("DOI") or "").lower() or None

    authors = []
    for a in m.get("author") or []:
        name = " ".join(x for x in [a.get("given"), a.get("family")] if x).strip()
        if name:
            authors.append(name)

    journal = (m.get("container-title") or [None])[0]
    issns = m.get("ISSN") or []
    pub = _best_date(m)

    return {
        "kind": "paper",
        "dedup_key": f"doi:{doi}" if doi else None,
        "doi": doi,
        "title": re.sub(r"\s+", " ", title).strip(),
        "abstract": clean_abstract(m.get("abstract")),
        "authors": authors,
        "journal": journal,
        "issn": issns[0] if issns else None,
        "published_at": pub,
        "url": m.get("URL") or (f"https://doi.org/{doi}" if doi else None),
        "source": source,
        "source_ref": doi,
    }


def fetch_by_doi(doi: str, mailto: str = "") -> dict | None:
    params: dict[str, Any] = {}
    if mailto:
        params["mailto"] = mailto
    data = get_json(f"{BASE}/works/{doi}", params=params or None)
    if not data:
        return None
    return _msg_to_item(data["message"])


# Crossref 支持 filter=doi:A,doi:B,... 一次查多篇。分片控制 URL 长度。
BATCH_SIZE = 50
_SELECT = ("DOI,title,container-title,issued,published-online,published-print,"
           "created,author,abstract,ISSN,URL")


def fetch_many_by_doi(dois: list[str], mailto: str = "") -> dict[str, dict]:
    """批量按 DOI 查询,返回 ``{doi: record}``。

    实测 ``filter=doi:A,doi:B``(重复过滤器名)有效,而 ``filter=doi:A,B`` 会报错,
    与 ISSN 过滤的坑一样。
    """
    out: dict[str, dict] = {}
    ids = [d.lower() for d in dict.fromkeys(dois) if d]
    for start in range(0, len(ids), BATCH_SIZE):
        chunk = ids[start:start + BATCH_SIZE]
        params: dict[str, Any] = {
            "filter": ",".join(f"doi:{d}" for d in chunk),
            "rows": len(chunk),
            "select": _SELECT,
        }
        if mailto:
            params["mailto"] = mailto
        data = get_json(f"{BASE}/works", params=params)
        if not data:
            continue
        for m in data.get("message", {}).get("items", []):
            it = _msg_to_item(m)
            if it and it.get("doi"):
                out[it["doi"]] = it
    return out


def search(
    query: str,
    *,
    lookback_days: int = 14,
    limit: int = 100,
    issns: list[str] | None = None,
    mailto: str = "",
) -> list[dict]:
    filters = [f"from-created-date:{days_ago(lookback_days)}"]
    # 注意:Crossref 多值过滤必须**重复写过滤器名**(issn:A,issn:B),
    # 写成 issn:A,B 会被解析成单个非法 ISSN 而报错。
    for i in issns or []:
        filters.append(f"issn:{i}")

    params: dict[str, Any] = {
        "query.bibliographic": query,
        "filter": ",".join(filters),
        "rows": min(limit, 1000),
        "select": ("DOI,title,container-title,issued,published-online,published-print,"
                   "created,author,abstract,ISSN,URL"),
    }
    if mailto:
        params["mailto"] = mailto

    data = get_json(f"{BASE}/works", params=params)
    if not data:
        return []
    out = []
    for m in data.get("message", {}).get("items", []):
        it = _msg_to_item(m)
        if it:
            out.append(it)
    return out
