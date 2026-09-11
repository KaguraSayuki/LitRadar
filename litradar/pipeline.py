"""流水线编排:ingest → enrich → rank → summarize。"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from . import db, enrich, rank, summarize
from .config import Config
from .normalize import title_norm
from .rank import load_interests
from .sources import crossref_search, mail, openalex_search, semanticscholar, xmol_email


# ------------------------------------------------------------------ ingest
def _prepare(data: dict) -> dict | None:
    """补 dedup_key / title_norm,缺 DOI 时退化为标题键。"""
    if not data.get("title"):
        return None
    if not data.get("title_norm"):
        data["title_norm"] = title_norm(data["title"])
    if not data.get("dedup_key"):
        doi = data.get("doi")
        data["dedup_key"] = (f"doi:{doi}" if doi
                             else f"title:{data['title_norm'][:120]}")
    return data


def _store(conn: sqlite3.Connection, data: dict) -> tuple[int, bool]:
    extras = {k: data.pop(k) for k in list(data) if k.startswith("_")}
    data = _prepare(data)
    if data is None:
        raise ValueError("缺少标题")
    iid, created = db.upsert_item(conn, data)
    if extras and any(v is not None for v in extras.values()):
        db.save_enrichment(conn, iid, {
            "cited_by_count": extras.get("_cited_by_count"),
            "is_oa": extras.get("_is_oa"),
            "oa_url": extras.get("_oa_url"),
            "openalex_id": extras.get("_openalex_id"),
        })
    return iid, created


def ingest_mail(cfg: Config, *, verbose: bool = True) -> dict:
    """解析 X-MOL 订阅邮件。支持 folder / maildir / imap 三种来源。"""
    database = db.Database(cfg.db_file)
    conn = database.connect()
    stat = {"messages": 0, "records": 0, "new": 0, "updated": 0, "errors": 0,
            "mode": cfg.mail.mode}
    started = db.now()
    try:
        for msg in mail.iter_messages(cfg.mail):
            stat["messages"] += 1
            try:
                records, meta = xmol_email.parse_bytes(msg.raw)
            except Exception as e:  # noqa: BLE001
                stat["errors"] += 1
                if verbose:
                    print(f"  [warn] 解析失败 {msg.source_ref}: {e}")
                continue

            if not records:
                # 不是 X-MOL 订阅邮件:仍留档,但不入库
                continue

            mid = meta.get("message_id") or msg.source_ref
            dup = conn.execute(
                "SELECT id FROM raw_email WHERE message_id = ?", (mid,)
            ).fetchone()
            if dup:
                if verbose:
                    print(f"  跳过已处理邮件 {mid}")
                mail.acknowledge(cfg.mail, msg)
                continue

            conn.execute(
                """INSERT OR IGNORE INTO raw_email
                   (message_id, received_at, subject, raw, parsed_at, parse_version, items_found)
                   VALUES (?,?,?,?,?,?,?)""",
                (mid, meta.get("received_at"), meta.get("subject"), msg.raw,
                 db.now(), xmol_email.PARSE_VERSION, len(records)),
            )
            conn.commit()

            for rec in records:
                stat["records"] += 1
                try:
                    _, created = _store(conn, xmol_email.to_item_data(rec, source_ref=mid))
                    stat["new" if created else "updated"] += 1
                except Exception as e:  # noqa: BLE001
                    stat["errors"] += 1
                    if verbose:
                        print(f"  [warn] 入库失败 {rec.title[:50]}: {e}")
            conn.commit()
            mail.acknowledge(cfg.mail, msg)
            if verbose:
                print(f"  邮件 {mid[:40]}… -> {len(records)} 条")

        db.log_run(conn, "ingest_mail", "ok", stat, started_at=started)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        db.log_run(conn, "ingest_mail", "failed", stat, error=str(e), started_at=started)
        conn.commit()
        raise
    finally:
        conn.close()
    return stat


def ingest_keyword_search(cfg: Config, *, verbose: bool = True) -> dict:
    """用 interests.yaml 里的检索词做全量召回。

    Crossref 为主力(免费无预算限制);OpenAlex 仅在配了 API Key 时启用
    —— 它 2026 年起改为额度制,未配 key 会直接返回 "Insufficient budget"。
    """
    prof = load_interests(cfg)
    issns: list[str] = []
    jr = (cfg.interests_data.get("journals") or {})
    issn_map = jr.get("issn") or {}
    for name in list(jr.get("core") or []) + list(jr.get("ok") or []):
        issns += issn_map.get(name) or []

    database = db.Database(cfg.db_file)
    conn = database.connect()
    queries = prof.queries or ([prof.query] if prof.query else [])
    stat: dict[str, Any] = {"queries": queries, "s2_queries": prof.s2_queries,
                            "per_query": {}, "crossref": 0, "s2": 0,
                            "unique": 0, "openalex": 0, "new": 0, "updated": 0,
                            "errors": 0}
    started = db.now()
    try:
        raw: list[dict] = []

        if cfg.sources.crossref_enabled:
            # 多查询并集 —— 实测单查询 100 篇 → 4 条查询并集 186 篇(+86%),
            # 收益在**召回**而不在精确率(精确率反而略降),精确交给 LLM 精排。
            for q in queries:
                got = crossref_search.search(
                    q,
                    lookback_days=cfg.sources.crossref_lookback_days,
                    limit=cfg.sources.crossref_rows,
                    issns=issns or None,
                    mailto=cfg.sources.mailto,
                )
                stat["per_query"][q] = len(got)
                raw += got
            stat["crossref"] = len(raw)

        # Semantic Scholar bulk:第三条腿。与 Crossref 互补 —— Crossref 模糊匹配、
        # 召回高噪声大;bulk 是精确 AND、召回低但准确率高。用 DOI 合并。
        if cfg.sources.s2_search_enabled and prof.s2_queries:
            if not os.environ.get("S2_API_KEY"):
                stat["s2_skipped"] = "未设 S2_API_KEY"
            else:
                for q in prof.s2_queries:
                    got = semanticscholar.search_bulk(
                        q,
                        year=cfg.sources.s2_search_year or None,
                        max_pages=cfg.sources.s2_search_max_pages,
                        interval=cfg.sources.s2_min_interval,
                        verbose=verbose,
                    )
                    stat["per_query"][f"[S2] {q}"] = len(got)
                    stat["s2"] += len(got)
                    raw += got

        if cfg.sources.openalex_enabled and os.environ.get(cfg.sources.openalex_api_key_env):
            for q in queries:
                got = openalex_search.search(
                    q,
                    lookback_days=cfg.sources.openalex_lookback_days,
                    limit=cfg.sources.openalex_per_query,
                    issns=issns or None,
                    mailto=cfg.sources.mailto,
                )
                stat["openalex"] += len(got)
                raw += got
        elif cfg.sources.openalex_enabled:
            stat["openalex_skipped"] = f"环境变量 {cfg.sources.openalex_api_key_env} 未设置"

        # 入库前去重:Crossref 同一 DOI 可能被多条查询、甚至同一响应重复返回,
        # 不去重会白白多花 LLM 精排的钱。
        seen: set[str] = set()
        found: list[dict] = []
        for data in raw:
            key = (data.get("doi") or "").lower() or (data.get("title") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            found.append(data)
        stat["unique"] = len(found)

        for data in found:
            try:
                _, created = _store(conn, data)
                stat["new" if created else "updated"] += 1
            except Exception as e:  # noqa: BLE001
                stat["errors"] += 1
                if verbose:
                    print(f"  [warn] 入库失败: {e}")
        conn.commit()
        db.log_run(conn, "ingest_search", "ok", stat, started_at=started)
        conn.commit()
        if verbose:
            print(f"  {len(queries)} 条查询 → 原始 {len(raw)} → 去重 {stat['unique']} "
                  f"→ 新增 {stat['new']} / 更新 {stat['updated']}")
    except Exception as e:  # noqa: BLE001
        db.log_run(conn, "ingest_search", "failed", stat, error=str(e), started_at=started)
        conn.commit()
        raise
    finally:
        conn.close()
    return stat


# --------------------------------------------------------------- 编排入口
def run_all(cfg: Config, *, days: int = 30, verbose: bool = True) -> dict:
    out: dict[str, Any] = {}
    if verbose:
        print("[1/4] 解析 X-MOL 订阅邮件")
    out["ingest_mail"] = ingest_mail(cfg, verbose=verbose)

    if verbose:
        print("[2/4] 关键词检索(OpenAlex / Crossref)")
    out["ingest_search"] = ingest_keyword_search(cfg, verbose=verbose)

    if verbose:
        print("[3/4] 富化(补摘要/引用数)")
    out["enrich"] = enrich.run(cfg, verbose=verbose)

    if verbose:
        print("[4/4] 排序 + 摘要")
    out["rank"] = rank.run(cfg, days=days, verbose=verbose)
    out["summarize"] = summarize.run(cfg, days=days, verbose=verbose)
    return out
