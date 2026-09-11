"""富化:用 DOI 补齐摘要、引用数、OA 链接。

分工(全部为实测结论):
  * Crossref          —— 元数据最权威;但 ACS 系期刊常缺摘要
  * Semantic Scholar  —— **摘要主力**,实测 4/4 ACS 论文都有;免费
                         用 **batch 端点**(一次最多 500 篇)把请求量压到最小
  * OpenAlex          —— 可选。2026 年起改为 API Key + 额度制,
                         不配 key 会返回 "Insufficient budget",故默认关闭
"""
from __future__ import annotations

import json
import os
import sqlite3

from . import db
from .config import Config
from .sources import crossref_search, openalex_search, semanticscholar


def _merge(doi: str, cr: dict | None, s2: dict | None, oa: dict | None) -> dict | None:
    """把三个来源合并成 {meta, patch}。摘要优先级 S2 > OpenAlex > Crossref。"""
    if not any((cr, s2, oa)):
        return None

    meta: dict = {
        "cited_by_count": None, "is_oa": None, "oa_url": None,
        "openalex_id": None, "openalex_json": None, "crossref_json": None,
    }
    patch: dict = {}

    if s2:
        if s2.get("abstract"):
            patch["abstract"] = s2["abstract"]
        if s2.get("cited_by_count") is not None:
            meta["cited_by_count"] = s2["cited_by_count"]
        if s2.get("oa_url"):
            meta["oa_url"] = s2["oa_url"]
            meta["is_oa"] = 1
        if s2.get("journal"):
            patch.setdefault("journal", s2["journal"])

    if oa:
        meta.update(
            cited_by_count=oa.get("_cited_by_count"),
            is_oa=oa.get("_is_oa"),
            oa_url=oa.get("_oa_url"),
            openalex_id=oa.get("_openalex_id"),
            openalex_json=json.dumps(oa, ensure_ascii=False, default=str),
        )
        if oa.get("abstract") and not patch.get("abstract"):
            patch["abstract"] = oa["abstract"]

    if cr:
        meta["crossref_json"] = json.dumps(cr, ensure_ascii=False, default=str)
        if cr.get("abstract") and not patch.get("abstract"):
            patch["abstract"] = cr["abstract"]
        # 期刊全称、ISSN、作者以 Crossref 为准(最规范)
        if cr.get("journal"):
            patch["journal"] = cr["journal"]
        if cr.get("issn"):
            patch["issn"] = cr["issn"]
        if cr.get("authors"):
            patch["authors"] = cr["authors"]

    return {"meta": meta, "patch": patch}




def _pending(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """需要富化的条目:从未富化的 + 已富化但缺摘要的。"""
    rows = conn.execute(
        """SELECT i.id, i.doi FROM item i
           LEFT JOIN item_enrichment e ON e.item_id = i.id
           WHERE i.doi IS NOT NULL AND i.doi <> '' AND e.item_id IS NULL
           ORDER BY i.published_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    rows += conn.execute(
        """SELECT i.id, i.doi FROM item i
           JOIN item_enrichment e ON e.item_id = i.id
           WHERE (i.abstract IS NULL OR i.abstract = '')
             AND i.doi IS NOT NULL AND i.doi <> ''
           ORDER BY i.published_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    seen: set[int] = set()
    out = []
    for r in rows:
        if int(r["id"]) not in seen:
            seen.add(int(r["id"]))
            out.append(r)
    return out


def run(cfg: Config, *, limit: int = 300, verbose: bool = True) -> dict:
    """对缺摘要或未富化的条目做一次富化。

    两个源都走批量端点,请求数从"每条 2 次"降到"每 50-100 条 1 次":
      * Semantic Scholar ``POST /paper/batch`` —— 限流瓶颈(1 req/s),收益最大
      * Crossref ``filter=doi:A,doi:B``       —— 一次查一批
    这样即使遵守 1 req/s 的入门限流,几千条也能在几十秒内跑完。
    """
    database = db.Database(cfg.db_file)
    conn = database.connect()
    started = db.now()
    stat = {"checked": 0, "enriched": 0, "with_abstract": 0, "failed": 0,
            "s2_batches": 0, "s2_hits": 0, "cr_batches": 0, "cr_hits": 0}

    try:
        rows = _pending(conn, limit)
        dois = [r["doi"] for r in rows]

        # ---- 1. 批量取 Semantic Scholar(摘要主力)----
        s2_map: dict[str, dict] = {}
        if cfg.sources.s2_enabled and dois:
            for s in range(0, len(dois), semanticscholar.BATCH_SIZE):
                chunk = dois[s:s + semanticscholar.BATCH_SIZE]
                if verbose:
                    print(f"  S2 批量 {s + 1}-{s + len(chunk)} / {len(dois)}…")
                s2_map.update(semanticscholar.fetch_many_by_doi(
                    chunk, interval=cfg.sources.s2_min_interval, verbose=verbose))
                stat["s2_batches"] += 1
            stat["s2_hits"] = len(s2_map)

        # ---- 2. 批量取 Crossref(元数据/期刊全称/ISSN)----
        cr_map: dict[str, dict] = {}
        if cfg.sources.crossref_enabled and dois:
            for s in range(0, len(dois), crossref_search.BATCH_SIZE):
                chunk = dois[s:s + crossref_search.BATCH_SIZE]
                if verbose:
                    print(f"  Crossref 批量 {s + 1}-{s + len(chunk)} / {len(dois)}…")
                cr_map.update(crossref_search.fetch_many_by_doi(chunk, cfg.sources.mailto))
                stat["cr_batches"] += 1
            stat["cr_hits"] = len(cr_map)

        # ---- 3. 合并入库 ----
        for row in rows:
            iid, doi = int(row["id"]), row["doi"]
            stat["checked"] += 1
            res = _merge(doi, cr_map.get(doi), s2_map.get(doi), None)
            if not res:
                stat["failed"] += 1
                continue
            db.save_enrichment(conn, iid, res["meta"])
            sets, vals = [], []
            for k, v in res["patch"].items():
                if k == "authors" and isinstance(v, list):
                    v = json.dumps(v, ensure_ascii=False)
                if v not in (None, "", []):
                    sets.append(f"{k} = COALESCE(NULLIF({k}, ''), ?)")
                    vals.append(v)
            if sets:
                vals.append(iid)
                conn.execute(f"UPDATE item SET {', '.join(sets)} WHERE id = ?", vals)
            stat["enriched"] += 1
            if res["patch"].get("abstract"):
                stat["with_abstract"] += 1
        conn.commit()

        # 跑完再统计一次:还有多少条目缺摘要。
        # S2 共享池不稳定,批量可能整体失败 —— 这些条目会被"缺摘要"路径
        # 在下次 enrich 时自动重试,但必须让用户看见这个数字。
        stat["pending_abstract"] = conn.execute(
            """SELECT COUNT(*) FROM item
               WHERE kind='paper' AND (abstract IS NULL OR abstract='')"""
        ).fetchone()[0]
        stat["total_papers"] = conn.execute(
            "SELECT COUNT(*) FROM item WHERE kind='paper'"
        ).fetchone()[0]

        db.log_run(conn, "enrich", "ok", stat, started_at=started)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        db.log_run(conn, "enrich", "failed", stat, error=str(e), started_at=started)
        conn.commit()
        raise
    finally:
        conn.close()

    return stat
