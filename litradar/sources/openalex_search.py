"""OpenAlex 关键词检索与单篇富化。

OpenAlex 的价值:**它有摘要**(倒排索引格式),而 Crossref 里 ACS 系期刊
(JACS / Org. Lett. / JOC)常常没有摘要。实测 5/5 的 ACS 论文都能在这里补到。
"""
from __future__ import annotations

from typing import Any

from ..http import get_json
from ..normalize import days_ago, normalize_doi, parse_date

BASE = "https://api.openalex.org"


def deinvert(inverted: dict[str, list[int]] | None) -> str | None:
    """把 OpenAlex 的 abstract_inverted_index 还原成正文。"""
    if not inverted:
        return None
    pos: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            pos[i] = word
    if not pos:
        return None
    return " ".join(pos[i] for i in sorted(pos))


def _work_to_item(w: dict, source: str = "openalex") -> dict | None:
    doi = normalize_doi(w.get("doi"))
    title = w.get("title") or w.get("display_name")
    if not title:
        return None

    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    journal = src.get("display_name")
    issn = None
    if src.get("issn_l"):
        issn = src["issn_l"]
    elif src.get("issn"):
        issn = (src["issn"] or [None])[0]

    authors = [
        (a.get("author") or {}).get("display_name")
        for a in (w.get("authorships") or [])
    ]
    authors = [a for a in authors if a]

    best = w.get("best_oa_location") or {}
    return {
        "kind": "paper",
        "dedup_key": f"doi:{doi}" if doi else None,
        "doi": doi,
        "title": title,
        "abstract": deinvert(w.get("abstract_inverted_index")),
        "authors": authors,
        "journal": journal,
        "issn": issn,
        "published_at": parse_date(w.get("publication_date")),
        "url": loc.get("landing_page_url") or (f"https://doi.org/{doi}" if doi else None),
        "source": source,
        "source_ref": w.get("id"),
        "_cited_by_count": w.get("cited_by_count"),
        "_is_oa": 1 if w.get("open_access", {}).get("is_oa") else 0,
        "_oa_url": best.get("pdf_url") or best.get("landing_page_url"),
        "_openalex_id": (w.get("id") or "").rsplit("/", 1)[-1] or None,
    }


def fetch_by_doi(doi: str, mailto: str = "", api_key: str = "") -> dict | None:
    """单篇富化:拿摘要 / 引用数 / OA 链接。

    2026 年起 OpenAlex 改成 API Key + 额度制,不带 key 直接被拒
    ("Insufficient budget"),所以 key 必须真的发出去。
    """
    params: dict[str, Any] = {}
    if mailto:
        params["mailto"] = mailto
    if api_key:
        params["api_key"] = api_key
    w = get_json(f"{BASE}/works/doi:{doi}", params=params or None)
    if not w:
        return None
    return _work_to_item(w)


def search(
    query: str,
    *,
    lookback_days: int = 14,
    limit: int = 60,
    issns: list[str] | None = None,
    mailto: str = "",
    api_key: str = "",
) -> list[dict]:
    """按关键词检索。查询词来自 interests.yaml,不是固定检索式。"""
    filters = [f"from_publication_date:{days_ago(lookback_days)}"]
    if issns:
        filters.append("primary_location.source.issn:" + "|".join(issns))

    params: dict[str, Any] = {
        "search": query,
        "filter": ",".join(filters),
        "per-page": min(limit, 200),
        "sort": "publication_date:desc",
    }
    if mailto:
        params["mailto"] = mailto
    if api_key:
        params["api_key"] = api_key

    data = get_json(f"{BASE}/works", params=params)
    if not data:
        return []
    out = []
    for w in data.get("results", []):
        it = _work_to_item(w)
        if it:
            out.append(it)
    return out
