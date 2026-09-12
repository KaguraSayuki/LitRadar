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
from .sources import crossref_search, easyscholar, openalex_search, semanticscholar


def _merge(doi: str, cr: dict | None, s2: dict | None, oa: dict | None) -> dict | None:
    """把三个来源合并成 {meta, patch, overwrite}。摘要优先级 S2 > OpenAlex > Crossref。

    字段分两类,语义不同:
      * ``patch``     —— **只填空**:库里已有值就不动。摘要属于这类(哪个源先
                        给到就用哪个,没有高下之分);S2 的刊名也属于这类,
                        它给的常是缩写。
      * ``overwrite`` —— **直接覆盖**:Crossref 的刊名/ISSN/作者最规范,来了
                        就该盖掉旧值。以前这些也只填空,于是邮件带进来的
                        "Org. Lett." 永远换不成全称,按刊名查 easyScholar
                        期刊等级就一直查不到。
    """
    if not any((cr, s2, oa)):
        return None

    meta: dict = {
        "cited_by_count": None, "is_oa": None, "oa_url": None,
        "openalex_id": None, "openalex_json": None, "crossref_json": None,
    }
    patch: dict = {}
    overwrite: dict = {}

    if s2:
        if s2.get("abstract"):
            patch["abstract"] = s2["abstract"]
        if s2.get("cited_by_count") is not None:
            meta["cited_by_count"] = s2["cited_by_count"]
        if s2.get("oa_url"):
            meta["oa_url"] = s2["oa_url"]
            meta["is_oa"] = 1
        if s2.get("journal"):
            patch.setdefault("journal", db.clean_journal(s2["journal"]))

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
        # 期刊全称、ISSN、作者以 Crossref 为准(最规范),所以进 overwrite。
        # 刊名要洗:Crossref 的 container-title 带 HTML 实体和换行,以前只有
        # 入库口 upsert_item 洗,富化直写会把脏名字带回库里。
        if cr.get("journal"):
            overwrite["journal"] = db.clean_journal(cr["journal"])
            patch.pop("journal", None)          # 同一字段别在 SET 里出现两次
        if cr.get("issn"):
            overwrite["issn"] = cr["issn"]
        if cr.get("authors"):
            overwrite["authors"] = cr["authors"]

    return {"meta": meta, "patch": patch, "overwrite": overwrite}




def _sql_value(key: str, value):
    """authors 在库里是 JSON 文本,其余字段原样写。"""
    if key == "authors" and isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return value


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


def _journal_ranks(conn, cfg: Config, *, verbose: bool = True) -> dict:
    """补齐还没缓存过的期刊等级。返回统计信息。

    接口按次计额,所以:
      · 查过的刊不重复查 —— hit=0 表示接口明确说没有,不该反复问
        (例外:后来给它配了别名,那是新线索,值得再试一次)
      · 单次运行有上限(cfg.journal_rank.max_lookups),异常数据打不爆额度
    """
    stat = {"jr_checked": 0, "jr_found": 0, "jr_missing": 0}
    if not cfg.journal_rank.enabled or not easyscholar.available():
        return stat

    aliases = {db.norm_journal(k): v
               for k, v in (cfg.journal_rank.aliases or {}).items()}

    # 候选 = 从没查过的 + (上次没查到、但现在配了别名的)。
    # 后一条不能省:实测先跑了没有别名的一轮,"Youji huaxue" 被记成查不到,
    # 之后再加别名也永远不会重试 —— 缓存把修复路径一起锁死了。
    need: list[str] = []
    for r in conn.execute(
            """SELECT DISTINCT i.journal AS journal, jr.hit AS hit
               FROM item i
               LEFT JOIN journal_rank jr ON jr.journal_norm = lower(trim(i.journal))
               WHERE i.journal IS NOT NULL AND i.journal <> ''"""):
        if r["hit"] is None or (r["hit"] == 0
                                and db.norm_journal(r["journal"]) in aliases):
            need.append(r["journal"])
    need = sorted(set(need))[:max(0, cfg.journal_rank.max_lookups)]

    for name in need:
        # 短名/罗马字名先换成 easyScholar 认得的全名再查,并按全名缓存
        target = aliases.get(db.norm_journal(name), name)
        ranks = easyscholar.fetch_rank(target)
        db.save_journal_rank(conn, target, ranks)
        stat["jr_checked"] += 1
        if ranks:
            stat["jr_found"] += 1
            if verbose:
                print(f"  期刊等级 {target}: {ranks.get('sciUp') or ranks.get('sci') or ''}")
        else:
            stat["jr_missing"] += 1
            if verbose:
                print(f"  [warn] 期刊等级没查到: {target}")
        conn.commit()          # 逐条提交:中断也不会白白浪费已花掉的额度
    return stat


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
            # overwrite:Crossref 的规范字段,非空就直接盖掉旧值
            for k, v in res["overwrite"].items():
                v = _sql_value(k, v)
                if v not in (None, "", []):
                    sets.append(f"{k} = ?")
                    vals.append(v)
            # patch:只在旧值为空时填,不动用户/更早来源已有的内容
            for k, v in res["patch"].items():
                v = _sql_value(k, v)
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

        # ---- 4. 期刊等级(easyScholar)----
        # 影响因子/分区 Crossref 和 S2 都不给,只能另找。按**刊名**查,
        # 所以这一步和逐条富化无关:全库也就几十本刊,查一遍进缓存,之后全走本地。
        stat.update(_journal_ranks(conn, cfg, verbose=verbose))

        db.log_run(conn, "enrich", "ok", stat, started_at=started)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        db.log_run(conn, "enrich", "failed", stat, error=str(e), started_at=started)
        conn.commit()
        raise
    finally:
        conn.close()

    return stat
