"""流水线编排:ingest → enrich → rank → summarize。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db, enrich, rank, summarize
from .config import Config, read_secret
from .normalize import normalize_doi, title_norm
from .rank import (Profile, load_enabled_groups, load_groups,
                   load_interests)
from .sources import crossref_search, mail, openalex_search, semanticscholar, xmol_email


# ------------------------------------------------------------------ ingest
def _prepare(data: dict) -> dict | None:
    """补 dedup_key / title_norm,缺 DOI 时退化为标题键。"""
    if not data.get("title"):
        return None
    # 所有来源都经过这里再入库。DOI 的大小写不是语义的一部分，且来源
    # 对 ``dedup_key`` 的填写并不一致（S2 以前会保留原始大小写），所以
    # 以归一化后的 DOI 重新生成 key，而不是只修 data["doi"]。
    doi = normalize_doi(data.get("doi"))
    if doi:
        data["doi"] = doi
        data["dedup_key"] = f"doi:{doi}"
    elif isinstance(data.get("dedup_key"), str) \
            and data["dedup_key"].lower().startswith("doi:"):
        doi = normalize_doi(data["dedup_key"])
        if doi:
            data["doi"] = doi
            data["dedup_key"] = f"doi:{doi}"
    if not data.get("title_norm"):
        data["title_norm"] = title_norm(data["title"])
    if not data.get("dedup_key"):
        data["dedup_key"] = (f"doi:{doi}" if doi
                             else f"title:{data['title_norm'][:120]}")
    return data


def _store(conn: sqlite3.Connection, data: dict, *,
           group_id: int | None = None) -> tuple[int, bool]:
    """入库唯一入口。``group_id`` 给了就把这篇记进该组。

    成员关系决定"它出现在哪个组的收件箱里",所以凡是分组采集都必须带上它。
    """
    extras = {k: data.pop(k) for k in list(data) if k.startswith("_")}
    data = _prepare(data)
    if data is None:
        raise ValueError("缺少标题")
    iid, created = db.upsert_item(conn, data)
    if group_id is not None:
        db.add_to_group(conn, group_id, iid)
    if extras and any(v is not None for v in extras.values()):
        db.save_enrichment(conn, iid, {
            "cited_by_count": extras.get("_cited_by_count"),
            "is_oa": extras.get("_is_oa"),
            "oa_url": extras.get("_oa_url"),
            "openalex_id": extras.get("_openalex_id"),
        }, partial=True)
    return iid, created


def ingest_mail(cfg: Config, *, verbose: bool = True) -> dict:
    """解析 X-MOL 订阅邮件。支持 folder / maildir / imap 三种来源。"""
    database = db.Database(cfg.db_file)
    conn = database.connect()
    # X-MOL 是**全局来源**:推什么由 X-MOL 网站上的订阅决定,本项目没法按方向
    # 驱动它。所以它的条目记进所有启用的组,再由各组的规则与关键词决定相关性。
    group_ids = db.sync_groups(conn, load_groups(cfg))
    mail_group_ids = [group_ids[g.slug] for g in load_enabled_groups(cfg)
                      if g.slug in group_ids]
    stat = {"messages": 0, "records": 0, "new": 0, "updated": 0, "errors": 0,
            "mode": cfg.mail.mode}
    started = db.now()
    try:
        def ingest_records(records, meta, raw: bytes, *, mid: str,
                           live_msg=None) -> None:
            """在一个事务中写入一封邮件；live_msg 存在时成功后才 ack。"""
            if not records:
                # 不是 X-MOL 订阅邮件:仍留档,但不入库
                return
            dup = conn.execute(
                "SELECT id, processed_at FROM raw_email WHERE message_id = ?", (mid,)
            ).fetchone()
            # processed_at 是在全部 item 成功后、同一个事务里写入的。老版本
            # 没有这个列/值的 raw_email 会再处理一次，从而修复“raw 已提交、
            # item 只落了一半”的历史状态；不按 source_ref 计数，因为同一 DOI
            # 可能已由别的来源入库，或一封邮件可能包含重复 DOI。
            complete = bool(dup and dup[1])
            if complete:
                if verbose:
                    print(f"  跳过已处理邮件 {mid}")
                if live_msg is not None:
                    mail.acknowledge(cfg.mail, live_msg)
                return

            # raw_email 是“处理完成”标记的一部分。它与该邮件的所有 item
            # 共用一个 savepoint，任何一条失败都会撤销整封邮件的写入；
            # savepoint 结束后才提交，外部邮件确认仍在提交之后。
            stat["records"] += len(records)
            message_new = message_updated = 0
            conn.execute("SAVEPOINT ingest_mail_message")
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO raw_email
                       (message_id, received_at, subject, raw, parsed_at, processed_at,
                        parse_version, items_found)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (mid, meta.get("received_at"), meta.get("subject"), raw,
                     db.now(), None, xmol_email.PARSE_VERSION, len(records)),
                )

                for rec in records:
                    iid, created = _store(conn, xmol_email.to_item_data(rec, source_ref=mid))
                    for gid in mail_group_ids:
                        db.add_to_group(conn, gid, iid)
                    if created:
                        message_new += 1
                    else:
                        message_updated += 1
                conn.execute("UPDATE raw_email SET processed_at=? WHERE message_id=?",
                             (db.now(), mid))
                conn.execute("RELEASE SAVEPOINT ingest_mail_message")
            except Exception as e:  # noqa: BLE001
                # rollback before logging/continuing: a later run_log commit must
                # never accidentally commit a partially processed email.
                conn.execute("ROLLBACK TO SAVEPOINT ingest_mail_message")
                conn.execute("RELEASE SAVEPOINT ingest_mail_message")
                stat["errors"] += 1
                if verbose:
                    title = records[0].title[:50] if records else mid[:50]
                    print(f"  [warn] 入库失败 {title}: {e}")
                # No acknowledge: the mail source can present it again.
                return

            # 只有所有条目已在 savepoint 中成功后才确认邮件。若 commit 或
            # acknowledge 抛错，外层异常处理会 rollback/留待下次重试。
            stat["new"] += message_new
            stat["updated"] += message_updated
            conn.commit()
            if live_msg is not None:
                mail.acknowledge(cfg.mail, live_msg)
            if verbose:
                print(f"  邮件 {mid[:40]}… -> {len(records)} 条")

        # 迁移前版本在写 raw_email 后就提交，且 IMAP 可能已经把邮件标为
        # Seen/归档，之后不会再从 iter_messages 返回。先离线重放所有尚未
        # processed_at 的原文；成功后才把完成标记写回，同样不需要外部 ack。
        pending = conn.execute(
            "SELECT message_id, received_at, subject, raw FROM raw_email "
            "WHERE processed_at IS NULL ORDER BY id"
        ).fetchall()
        for old in pending:
            stat["messages"] += 1
            try:
                records, meta = xmol_email.parse_bytes(old["raw"])
            except Exception as e:  # noqa: BLE001
                stat["errors"] += 1
                if verbose:
                    print(f"  [warn] 历史邮件重放解析失败 {old['message_id']}: {e}")
                continue
            replay_meta = {
                "message_id": old["message_id"],
                "received_at": old["received_at"] or meta.get("received_at"),
                "subject": old["subject"] or meta.get("subject"),
            }
            ingest_records(records, replay_meta, old["raw"], mid=old["message_id"])

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
            ingest_records(records, meta, msg.raw, mid=mid, live_msg=msg)

        db.log_run(conn, "ingest_mail", "ok", stat, started_at=started)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        # The failed transaction may contain a raw_email row or partial item;
        # discard it before recording the diagnostic run log.
        conn.rollback()
        db.log_run(conn, "ingest_mail", "failed", stat, error=str(e), started_at=started)
        conn.commit()
        raise
    finally:
        conn.close()
    return stat


def _ingest_snowball(conn: sqlite3.Connection, cfg: Config, prof: Profile,
                     group_id: int, stat: dict, *,
                     verbose: bool = True) -> list[dict]:
    """引用滚雪球:顺着自己领域的基础文献往前滚。

    精度靠**共被引计数**,不靠关键词:

        统计每篇候选被几个**不同**种子引用过。同时引用了我 2 个以上种子的
        论文几乎必然在同一脉络里;而碰巧引到 1 个的不相关论文(MOF、离子
        液体、纳米簇)不会。实测 11 条候选筛出 3 条,3 条全中;
        正式跑一轮 34 条筛出 5 条,4 条入库、3 条对口。

    为什么不用关键词卡:滚雪球的价值恰恰在于发现**换了说法**的新工作 ——
    拿自己的检索词去卡,等于把它最擅长的那部分挡在门外。共被引依据的是
    "这个领域的前辈们被谁共同引用",和我的词表无关。

    为什么要落表、而不是一轮查完所有种子:
      1. S2 免费 key 名义 1 req/s,实测连打十几个种子一半会 429。
         一轮只刷新"最久没查的"几个,轮着来(日报场景完全够)。
      2. 一次失败不影响判断 —— 共被引是在**全部种子、全部历史轮次**上统计的。
         否则那 5 个失败种子引用的论文永远凑不够票,会静默漏掉。
    """
    stat.update({"snowball_refreshed": 0, "snowball_failed": 0,
                 "snowball_new_cites": 0, "snowball_kept": 0})
    seeds = [s.strip() for s in (prof.seed_dois or []) if s and s.strip()]
    if not cfg.sources.snowball_enabled or not seeds:
        return []
    seeds = seeds[:cfg.sources.snowball_max_seeds]
    if not read_secret("S2_API_KEY"):
        stat["snowball_skipped"] = "未设 S2_API_KEY"
        return []

    from datetime import date, timedelta
    cutoff = date.today() - timedelta(days=cfg.sources.s2_search_lookback_days)
    yr = f"{cutoff.year}-{date.today().year}"

    # ---- 1. 刷新最久没查的几个种子 ----
    todo = db.pick_seeds_to_query(conn, seeds, cfg.sources.snowball_seeds_per_run,
                                 group_id=group_id)
    for seed in todo:
        got = semanticscholar.fetch_citations(
            f"DOI:{seed}", year=yr, since=cutoff.isoformat(),
            limit=cfg.sources.snowball_per_seed,
            interval=cfg.sources.s2_min_interval, verbose=verbose)
        if not got:
            # 记失败也更新时间戳:否则一个 404 的死种子会每轮都排在最前面
            db.mark_seed_queried(conn, seed, 0, group_id=group_id,
                                 status="failed")
            stat["snowball_failed"] += 1
            conn.commit()
            continue
        stat["snowball_new_cites"] += db.save_seed_cites(conn, seed, got,
                                                      group_id=group_id)
        db.mark_seed_queried(conn, seed, len(got), group_id=group_id)
        stat["snowball_refreshed"] += 1
        conn.commit()          # 逐种子提交:中断也不丢已花掉的额度

    # ---- 2. 在累积的引用关系上做共被引闸门 ----
    kept = db.cocited_items(conn, cfg.sources.snowball_min_cocitations,
                            group_id=group_id)
    stat["snowball_kept"] = len(kept)
    if verbose:
        total = conn.execute("SELECT COUNT(DISTINCT citing_doi) FROM seed_cite "
                             "WHERE group_id=?", (group_id,)).fetchone()[0]
        print(f"  滚雪球 刷新 {stat['snowball_refreshed']}/{len(todo)} 个种子"
              + (f"(失败 {stat['snowball_failed']})" if stat["snowball_failed"] else "")
              + f" · 累计引用关系 {total} 条"
              + f" → 共被引≥{cfg.sources.snowball_min_cocitations} 待入库 {len(kept)}")
    return kept


def _issns_for(prof: Profile) -> list[str]:
    """本组期刊白名单的 ISSN(Crossref / OpenAlex 的 issn 过滤用)。"""
    out: list[str] = []
    for name in list(prof.journals_core) + list(prof.journals_ok):
        out += prof.journal_issns.get(name) or []
    return out


def _ingest_group(conn, cfg: Config, prof: Profile, group_id: int, *,
                  verbose: bool = True) -> dict:
    """**一个组**的召回:Crossref / S2 bulk / S2 venue / OpenAlex / 滚雪球。

    命中的条目都记进这个组(item_group),这样"它出现在哪个组的收件箱"和
    "哪个组的规则给它打分"才是同一个事实。
    """
    issns = _issns_for(prof)
    queries = prof.queries or ([prof.query] if prof.query else [])
    stat: dict[str, Any] = {"queries": queries, "s2_queries": prof.s2_queries,
                            "per_query": {}, "crossref": 0, "s2": 0,
                            "snowball": 0, "unique": 0, "openalex": 0,
                            "raw": 0, "new": 0, "updated": 0, "errors": 0}
    started = db.now()
    try:
        raw: list[dict] = []

        if cfg.sources.crossref_search_enabled:
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
            if not read_secret("S2_API_KEY"):
                stat["s2_skipped"] = "未设 S2_API_KEY"
            else:
                # bulk 只有 year 粒度,没有日期;由 lookback 反推年份区间
                from datetime import date, timedelta
                cutoff = date.today() - timedelta(days=cfg.sources.s2_search_lookback_days)
                yr = cfg.sources.s2_search_year or f"{cutoff.year}-{date.today().year}"
                stat["s2_year"] = yr
                stat["s2_since"] = cutoff.isoformat()
                for q in prof.s2_queries:
                    got = semanticscholar.search_bulk(
                        q,
                        year=yr,
                        since=cutoff.isoformat(),
                        max_pages=cfg.sources.s2_search_max_pages,
                        interval=cfg.sources.s2_min_interval,
                        verbose=verbose,
                    )
                    stat["per_query"][f"[S2] {q}"] = len(got)
                    stat["s2"] += len(got)
                    raw += got

                # 期刊定向的宽查询:替代 Crossref 原来的 ISSN 白名单覆盖
                for q in prof.s2_venue_queries:
                    got = semanticscholar.search_bulk(
                        q,
                        year=yr,
                        since=cutoff.isoformat(),
                        venues=cfg.sources.s2_venues or None,
                        max_pages=cfg.sources.s2_search_max_pages,
                        interval=cfg.sources.s2_min_interval,
                        verbose=verbose,
                    )
                    stat["per_query"][f"[S2·venue] {q}"] = len(got)
                    stat["s2"] += len(got)
                    raw += got

        openalex_key = read_secret(cfg.sources.openalex_api_key_env) or ""
        if cfg.sources.openalex_enabled and openalex_key:
            for q in queries:
                got = openalex_search.search(
                    q,
                    lookback_days=cfg.sources.openalex_lookback_days,
                    limit=cfg.sources.openalex_per_query,
                    issns=issns or None,
                    mailto=cfg.sources.mailto,
                    api_key=openalex_key,
                )
                stat["openalex"] += len(got)
                raw += got
        elif cfg.sources.openalex_enabled:
            stat["openalex_skipped"] = f"环境变量 {cfg.sources.openalex_api_key_env} 未设置"

        # 引用滚雪球:第三条召回腿。前两条靠词表,这条靠领域前辈的引用关系 ——
        # 所以它能捞到"换了说法"的工作,那是关键词召回天然的盲区。
        got_snow = _ingest_snowball(conn, cfg, prof, group_id, stat, verbose=verbose)
        stat["snowball"] = len(got_snow)
        raw += got_snow
        stat["raw"] = len(raw)

        # 入库前去重:Crossref 同一 DOI 可能被多条查询、甚至同一响应重复返回,
        # 不去重会白白多花 LLM 精排的钱。
        seen: set[str] = set()
        found: list[dict] = []
        for data in raw:
            key = normalize_doi(data.get("doi")) or title_norm(data.get("title"))
            if not key or key in seen:
                continue
            seen.add(key)
            found.append(data)
        stat["unique"] = len(found)

        for data in found:
            try:
                _, created = _store(conn, data, group_id=group_id)
                stat["new" if created else "updated"] += 1
            except Exception as e:  # noqa: BLE001
                stat["errors"] += 1
                if verbose:
                    print(f"  [warn] 入库失败: {e}")
        conn.commit()
        db.log_run(conn, "ingest_search", "ok", stat, started_at=started,
                   group_slug=prof.slug)
        conn.commit()
        if verbose:
            print(f"  [{prof.name}] {len(queries)} 条查询 → 原始 {stat['raw']} → "
                  f"去重 {stat['unique']} → 新增 {stat['new']} / 更新 {stat['updated']}")
    except Exception as e:  # noqa: BLE001
        db.log_run(conn, "ingest_search", "failed", stat, error=str(e),
                   started_at=started, group_slug=prof.slug)
        conn.commit()
        raise
    return stat


def ingest_keyword_search(cfg: Config, *, verbose: bool = True,
                          group: Profile | None = None) -> dict:
    """按订阅组做全量召回。

    每个组用自己的检索词、期刊白名单与滚雪球种子各跑一遍;规则过滤与 BM25
    也按组各算(见 ``rank.run``)。``group`` 指定时只跑那一个组。

    **单个组失败不拖垮其它组**:一个方向配错了检索词,不该让另外两个方向
    当天颗粒无收。失败会被记进 ``groups[slug].error`` 与 ``errors`` 计数。
    """
    groups = [group] if group is not None else load_enabled_groups(cfg)
    conn = db.Database(cfg.db_file).connect()
    try:
        ids = db.sync_groups(conn, load_groups(cfg))
        conn.commit()
        total: dict[str, Any] = {"groups": {}, "crossref": 0, "s2": 0, "snowball": 0,
                                 "openalex": 0, "unique": 0, "new": 0,
                                 "updated": 0, "errors": 0}
        for prof in groups:
            gid = ids.get(prof.slug)
            if gid is None:
                continue
            try:
                stat = _ingest_group(conn, cfg, prof, gid, verbose=verbose)
            except Exception as e:  # noqa: BLE001
                total["errors"] += 1
                total["groups"][prof.slug] = {"error": f"{type(e).__name__}: {e}"}
                if verbose:
                    print(f"  [warn] 订阅组「{prof.name}」采集失败: "
                          f"{type(e).__name__}: {e}")
                continue
            total["groups"][prof.slug] = stat
            for key in ("crossref", "s2", "snowball", "openalex", "unique",
                        "new", "updated", "errors"):
                total[key] += int(stat.get(key, 0))
        return total
    finally:
        conn.close()


# --------------------------------------------------------------- 编排入口
def run_all(cfg: Config, *, days: int = 200, verbose: bool = True) -> dict:
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
