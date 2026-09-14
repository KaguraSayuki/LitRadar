"""结构化中文摘要 + 中文标题翻译。带数字核验,防止 LLM 编造实验数据。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from . import db, progress
from .config import Config
from .llm import LLMClient, LLMError, validate_items, validate_text
from .rank import Profile, load_enabled_groups, load_groups, load_interests

SUMMARY_SYSTEM = "你是严谨的化学文献助手,只输出 JSON,绝不编造数据。"

SUMMARY_PROMPT = """把下面这篇化学文献做成中文摘要。

【题录】
标题(英文):{title}
期刊:{journal}  发表日期:{published}
作者:{authors}
摘要原文:{abstract}

【用户研究方向】
{direction}

【硬性要求】
1. **数字绝不能编**:任何产率、ee、TOF、温度等数值,必须能在"摘要原文"中
   逐字找到。找不到就把该数字删掉,而不是猜
2. 其余内容可以基于摘要做**合理概括**(摘要通常不直说"解决了什么问题"),
   但凡属推断而非摘要明写的,句末加"(推断)"
3. 摘要完全没提到的信息,填 "摘要未提及";不要为了填满而编
4. 除专业术语外全部使用中文。术语保留英文原词(如 photoredox、
   aza-Claisen rearrangement、BOX ligand),不要硬译
5. 只输出 JSON

各字段要求:
- title_zh: 把英文标题准确译成中文学术标题,术语保留英文。这是**翻译**不是重写
- one_liner: 一句话结论,不超过 40 字
- problem: 这篇工作针对什么问题/空白。摘要没明说时,**从研究内容合理推断**并标注"(推断)"
- method: 关键方法与条件(催化剂/配体/底物范围)
- key_results: 关键结果。**即使没有具体数字也要描述发现**;有数字时数字必须与原文一致
- limitation: 作者自述的局限,或从摘要可合理推断的限制(标"(推断)")
- relevance: 对用户研究方向有什么用,1-2 句。若与该方向关系不大,直说

输出格式:
{{"title_zh":"中文标题",
 "one_liner":"一句话结论",
 "problem":"...",
 "method":"...",
 "key_results":"...",
 "limitation":"...",
 "relevance":"..."}}"""

BRIEF_SYSTEM = "你是化学文献助手,只输出 JSON,不编造。"

BRIEF_PROMPT = """为下列文献各输出「中文标题」和「一句话中文结论」。
只依据给出的标题和摘要,不得编造数据。
专业术语(如 photoredox、aza-Claisen)保留英文原词,不要硬译。
title_zh 是**翻译**,不要重写或添加原文没有的内容。

{items}

只输出 JSON:
{{"items":[{{"id":1,"title_zh":"中文标题","one_liner":"一句话结论,不超过40字"}}]}}"""

RELEVANCE_SYSTEM = "你是化学文献助手,只输出 JSON,不编造。"

# 只有"对研究的用处"与方向有关,所以已经算过中性摘要的条目,换一个组时
# 只需要补这一行 —— 不必把问题/方法/结果/局限整套重算一遍。
RELEVANCE_PROMPT = """判断这篇化学文献对用户的研究方向有什么用。

【题录】
标题(英文):{title}
摘要原文:{abstract}

【用户研究方向】
{direction}

要求:
1. 只输出 JSON,一个字段:{{"relevance":"1-2 句"}}
2. 说明这篇工作对这个方向有什么用;关系不大就直说,不要硬找关系
3. 不要编造摘要原文里没有的数据
4. 用中文,专业术语保留英文原词"""


def summarize_relevance(rows: list[sqlite3.Row], prof: Profile,
                        llm: LLMClient) -> dict[int, str]:
    """只补"对研究的用处"这一行。返回 {item_id: relevance}。

    整条摘要的中性部分已经算过了,换组重算整套会白花 LLM 的钱。
    """
    out: dict[int, str] = {}
    for index, row in enumerate(rows):
        progress.report(f'{prof.name} · 生成方向说明', index, len(rows))
        prompt = RELEVANCE_PROMPT.format(
            title=row["title"],
            abstract=(row["abstract"] or "")[:2000] or "(无摘要,只有标题)",
            direction=db.summary_direction(prof.direction),
        )
        try:
            data = llm.json(RELEVANCE_SYSTEM, prompt, max_tokens=300)
            text = validate_text(data, ("relevance",))["relevance"]
        except LLMError as e:
            if len(rows) == 1:
                raise
            print(f"  [warn] relevance 失败 #{row['id']}: {e}")
            continue
        finally:
            progress.report(f'{prof.name} · 方向说明，已处理 {index + 1} 篇', index + 1, len(rows))
        if text:
            out[int(row["id"])] = text
    return out


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_FIELDS = ("one_liner", "problem", "method", "key_results", "limitation", "relevance")


def _numbers(text: str) -> set[str]:
    return set(_NUM_RE.findall(text or ""))


def _abstract_hash(abstract: str | None) -> str:
    """记录生成时实际读取的原文,不把一次元数据刷新误当成摘要变化。"""
    return hashlib.sha256((abstract or "").encode("utf-8")).hexdigest()


# 历史摘要没有指纹/富化记录时仍要得到明确的布尔值,否则 stale 和 NOT stale
# 都可能为 SQL NULL,既不刷新摘要也不补新组说明。
_SUMMARY_STALE = """COALESCE((
    (su.abstract_hash IS NOT NULL
     AND su.abstract_hash <> litradar_abstract_hash(i.abstract))
    OR (su.abstract_hash IS NULL
        AND COALESCE(i.abstract, '') <> ''
        AND (e.enriched_at > su.created_at
             OR (su.depth = 'deep' AND su.problem = '摘要未提及'
                 AND su.method = '摘要未提及' AND su.key_results = '摘要未提及'
                 AND su.limitation = '摘要未提及')))
), 0)"""


def verify_numbers(summary: dict, abstract: str) -> dict:
    """key_results 里出现但摘要原文没有的数字 -> 标疑,避免幻觉数据流到界面。"""
    abs_nums = _numbers(abstract)
    kr = summary.get("key_results") or ""
    if not kr or kr == "摘要未提及":
        return summary
    bogus = [n for n in _numbers(kr) if n not in abs_nums]
    if bogus:
        summary["key_results"] = f"{kr}  ⚠️(数字 {'/'.join(bogus[:4])} 未在原文中找到,请核对)"
        summary["_unverified_numbers"] = bogus
    return summary


def _join_authors(row: sqlite3.Row, n: int = 6) -> str:
    try:
        return ", ".join(json.loads(row["authors"] or "[]")[:n])
    except (json.JSONDecodeError, TypeError):
        return row["authors"] or ""


def summarize_one(row: sqlite3.Row, prof: Profile, llm: LLMClient) -> dict:
    abstract = row["abstract"] or ""
    prompt = SUMMARY_PROMPT.format(
        title=row["title"], journal=row["journal"] or "未知",
        published=row["published_at"] or "未知", authors=_join_authors(row) or "未知",
        abstract=abstract[:3000] or "(无摘要,只有标题和期刊信息)",
        direction=db.summary_direction(prof.direction),
    )
    data = llm.json(SUMMARY_SYSTEM, prompt, max_tokens=1400)

    data = validate_text(data, ("title_zh", "one_liner"), _FIELDS[1:])
    out: dict = {"title_zh": data["title_zh"]}
    for k in _FIELDS:
        out[k] = (str(data.get(k) or "摘要未提及")).strip()

    if not abstract:
        # 没有摘要时不要假装能总结方法/数据
        for k in ("problem", "method", "key_results", "limitation"):
            out[k] = "摘要未提及"
        out["_no_abstract"] = True
    else:
        out = verify_numbers(out, abstract)
    return out


def summarize_brief(rows: list[sqlite3.Row], llm: LLMClient) -> dict[int, dict]:
    """批量:每条产出中文标题 + 一句话结论。"""
    blocks = []
    for i, r in enumerate(rows, 1):
        blocks.append(f"[{i}] 标题: {r['title']}\n    摘要: {(r['abstract'] or '(无摘要)')[:700]}")
    try:
        data = llm.json(BRIEF_SYSTEM, BRIEF_PROMPT.format(items="\n\n".join(blocks)),
                        max_tokens=2500)
        entries = validate_items(data.get("items") or data.get("summaries"), len(rows))
        out = {int(rows[entry["id"] - 1]["id"]):
               validate_text(entry, ("title_zh", "one_liner")) for entry in entries}
    except LLMError as e:
        print(f"  [warn] 批量摘要失败: {e}")
        return {}

    return out


def _summarize_group(conn: sqlite3.Connection, cfg: Config, prof: Profile,
                     group_id: int, llm: LLMClient, *, limit: int, days: int,
                     verbose: bool, force: bool) -> dict:
    """**一个组**的摘要。

    先生成中性摘要,同一轮共享条目只处理一次。全体组的中性摘要完成后,
    run 再补各组 relevance,以便前面的组也拿到后来升级为 deep 的说明。
    """
    stat: dict = {"group": prof.slug, "deep": 0, "brief": 0, "brief_failed": 0,
                  "skipped": 0, "relevance": 0}
    started = db.now()
    try:
        # 新摘要按原文指纹判断过期:包括无原文→有原文,也包括同一秒内补齐。
        # 历史摘要没有指纹,用富化时间或旧的无摘要占位内容恢复一次;
        # 成功重做后会保存指纹,之后仅引用数等元数据变化不会再触发 LLM。
        stale = _SUMMARY_STALE
        w = (f"-{days} days", f"-{days} days")

        # ---- A. 要整条重做的 ----
        where = "1=1" if force else f"(su.item_id IS NULL OR {stale})"
        rows = list(conn.execute(
            f"""SELECT i.*, COALESCE(sc.final_score,-1) AS fs, su.depth AS depth,
                       {stale} AS summary_stale,
                       i.id IN (SELECT item_id FROM temp.summary_run_deep) AS wants_deep
                FROM item i
                JOIN item_group ig ON ig.item_id = i.id AND ig.group_id = ?
                LEFT JOIN summary          su ON su.item_id = i.id
                LEFT JOIN score            sc ON sc.item_id = i.id AND sc.group_id = ?
                LEFT JOIN item_enrichment  e  ON e.item_id  = i.id
                WHERE {where} AND i.kind='paper' AND {db.in_window('i')}
                  AND i.id NOT IN (SELECT item_id FROM temp.summary_run_done)
                ORDER BY fs DESC, i.published_at DESC
                LIMIT ?""",
            (group_id, group_id, *w, limit),
        ).fetchall())
        stat["pending"] = len(rows)

        # 深度摘要跟着**排名**走,而不是跟着"这一轮 pending 的前 N"走。
        # 老写法有两个后果:条目排名后来上升时,它已经有 brief 摘要、不再是
        # pending,于是永远升不成深度摘要;而且每跑一轮,深度摘要都落在剩下的
        # pending 上,沿排名一路下漂 —— "前 N 篇深度摘要"名不副实。
        deep_n = cfg.llm.deep_summary_top_n
        top_rows = list(conn.execute(
            f"""SELECT i.*, COALESCE(sc.final_score,-1) AS fs, su.depth AS depth,
                       {stale} AS summary_stale,
                       i.id IN (SELECT item_id FROM temp.summary_run_done) AS refreshed
                FROM item i
                JOIN item_group ig ON ig.item_id = i.id AND ig.group_id = ?
                LEFT JOIN score   sc ON sc.item_id = i.id AND sc.group_id = ?
                LEFT JOIN summary su ON su.item_id = i.id
                LEFT JOIN item_enrichment e ON e.item_id = i.id
                WHERE i.kind='paper' AND {db.in_window('i')}
                ORDER BY fs DESC, i.published_at DESC
                LIMIT ?""",
            (group_id, group_id, *w, deep_n),
        ).fetchall()) if deep_n > 0 else []
        heads = [r for r in top_rows
                 if not r["refreshed"] and (force or r["depth"] != "deep" or r["summary_stale"])]
        head_ids = {int(r["id"]) for r in heads}
        for r in rows:
            if (r["depth"] == "deep" or r["wants_deep"]) and int(r["id"]) not in head_ids:
                heads.append(r)
                head_ids.add(int(r["id"]))
        rest = [r for r in rows if int(r["id"]) not in head_ids]

        for index, r in enumerate(heads):
            progress.report(f'{prof.name} · 生成深度摘要', index, len(heads))
            if verbose:
                print(f"  [{prof.name}] 深度摘要 #{r['id']}: {r['title'][:48]}…")
            try:
                data = summarize_one(r, prof, llm)
                iid = int(r["id"])
                db.save_summary(conn, iid, data, "deep", cfg.llm.model,
                                abstract_hash=_abstract_hash(r["abstract"]))
                # 说明只依赖原文和方向。原文变了才使其它组说明失效;
                # 单纯 force 重写中性摘要不能删除未参与本轮的有效说明。
                if r["summary_stale"]:
                    db.clear_group_relevance(conn, iid, keep_group_id=group_id)
                db.save_group_relevance(conn, group_id, iid,
                                        data.get("relevance"), cfg.llm.model)
                conn.execute("INSERT INTO temp.summary_run_done VALUES (?,?)", (iid, group_id))
                conn.commit()
                stat["deep"] += 1
            except LLMError as e:
                print(f"  [warn] 摘要失败: {e}")
                stat["skipped"] += 1
                stat["deep_failed"] = stat.get("deep_failed", 0) + 1
            finally:
                progress.report(f'{prof.name} · 深度摘要，已处理 {index + 1} 篇', index + 1, len(heads))

        bs = 10
        for s in range(0, len(rest), bs):
            chunk = rest[s:s + bs]
            progress.report(f'{prof.name} · 生成简要摘要，第 {s // bs + 1} 批', s, len(rest))
            if verbose:
                print(f"  [{prof.name}] 批量摘要 {s + 1}-{s + len(chunk)} / {len(rest)}…")
            brief = summarize_brief(chunk, llm)
            progress.report(f'{prof.name} · 简要摘要，已处理 {s + len(chunk)} 篇', s + len(chunk), len(rest))
            if not brief:
                stat["brief_failed"] += len(chunk)
                continue
            for r in chunk:
                iid = int(r["id"])
                if iid in brief:
                    db.save_summary(conn, iid, brief[iid], "brief", cfg.llm.model,
                                    abstract_hash=_abstract_hash(r["abstract"]))
                    conn.execute("INSERT INTO temp.summary_run_done VALUES (?,NULL)", (iid,))
                    stat["brief"] += 1
            conn.commit()

    except Exception as e:  # noqa: BLE001
        db.log_run(conn, "summarize", "failed", stat, error=str(e),
                   started_at=started, group_slug=prof.slug)
        conn.commit()
        raise
    return stat


def _complete_group_relevance(conn: sqlite3.Connection, cfg: Config, prof: Profile,
                              group_id: int, llm: LLMClient, stat: dict, *,
                              limit: int, days: int, verbose: bool, force: bool) -> None:
    """所有组的中性摘要落库后,只补/强制更新本组的方向性说明。"""
    stale = _SUMMARY_STALE
    w = (f"-{days} days", f"-{days} days")
    # ---- B. 只补"对研究的用处" ----
    need_rel = list(conn.execute(
        f"""SELECT i.*
            FROM item i
            JOIN item_group ig ON ig.item_id = i.id AND ig.group_id = ?
            JOIN summary      su ON su.item_id = i.id
            LEFT JOIN summary_group sg ON sg.item_id = i.id AND sg.group_id = ?
            LEFT JOIN item_enrichment e ON e.item_id = i.id
            WHERE i.kind='paper' AND su.depth='deep'
              AND NOT {stale}
              AND (sg.item_id IS NULL OR (? AND NOT EXISTS (
                    SELECT 1 FROM temp.summary_run_done done
                     WHERE done.item_id=i.id AND done.group_id=?)))
              AND {db.in_window('i')}
            ORDER BY i.published_at DESC
            LIMIT ?""",
        (group_id, group_id, force, group_id, *w, limit),
    ).fetchall())
    if need_rel:
        if verbose:
            print(f"  [{prof.name}] 补 relevance {len(need_rel)} 篇…")
        # 单独兜异常:上面的 deep/brief 已经提交,不能因为补 relevance 失败
        # 就把整组记成失败、把已经做成的统计一起丢掉。下一轮会再挑出来。
        try:
            got = summarize_relevance(need_rel, prof, llm)
        except Exception as e:  # noqa: BLE001
            stat["relevance_failed"] = len(need_rel)
            if verbose:
                print(f"  [warn] 补 relevance 失败({len(need_rel)} 篇): "
                      f"{type(e).__name__}: {e}")
            got = {}
        for iid, text in got.items():
            db.save_group_relevance(conn, group_id, iid, text, cfg.llm.model)
            stat["relevance"] += 1
        conn.commit()


def run(cfg: Config, *, limit: int = 200, days: int = 30, verbose: bool = True,
        force: bool = False, group: Profile | None = None,
        selected_groups: list[Profile] | None = None) -> dict:
    """按订阅组生成中文摘要。``group`` 指定时只跑那一个组。

    摘要里只有 **relevance**(对研究的用处)与方向有关,其余字段通用,所以中性
    部分一份共享(存在 summary),按组的那一行存在 summary_group —— 整条摘要
    按组重算会把 LLM 成本乘上组数。

    **一个组失败不影响其它组**:某个方向画像写坏了,不该让别的方向没摘要。
    """
    groups = (selected_groups if selected_groups is not None else
              [group] if group is not None else load_enabled_groups(cfg))
    llm = LLMClient(cfg.llm)
    if not llm.available:
        return {"skipped": "未配置 API key 或 LLM 已禁用"}

    conn = db.Database(cfg.db_file).connect()
    conn.create_function("litradar_abstract_hash", 1, _abstract_hash, deterministic=True)
    try:
        ids = db.sync_groups(conn, load_groups(cfg))
        conn.commit()
        # 先合并本轮各组的深度需求,避免 A 组先花钱生成 brief,B 组再升级。
        # 临时表随连接销毁;done 只记录成功保存的摘要,失败仍可由后续组重试。
        conn.execute("CREATE TEMP TABLE summary_run_deep (item_id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TEMP TABLE summary_run_done (item_id INTEGER PRIMARY KEY, group_id INTEGER)")
        if cfg.llm.deep_summary_top_n > 0:
            for prof in groups:
                gid = ids.get(prof.slug)
                if gid is not None:
                    conn.execute(
                        f"""INSERT OR IGNORE INTO temp.summary_run_deep
                            SELECT i.id FROM item i
                            JOIN item_group ig ON ig.item_id=i.id AND ig.group_id=?
                            LEFT JOIN score sc ON sc.item_id=i.id AND sc.group_id=?
                            WHERE i.kind='paper' AND {db.in_window('i')}
                            ORDER BY COALESCE(sc.final_score,-1) DESC, i.published_at DESC
                            LIMIT ?""",
                        (gid, gid, f"-{days} days", f"-{days} days", cfg.llm.deep_summary_top_n),
                    )
        conn.commit()
        total: dict = {"groups": {}, "deep": 0, "brief": 0, "brief_failed": 0,
                       "skipped": 0, "relevance": 0, "errors": 0}
        started = {}
        for prof in groups:
            gid = ids.get(prof.slug)
            if gid is None:
                continue
            started[prof.slug] = db.now()
            try:
                stat = _summarize_group(conn, cfg, prof, gid, llm, limit=limit,
                                        days=days, verbose=verbose, force=force)
            except Exception as e:  # noqa: BLE001
                total["errors"] += 1
                total["groups"][prof.slug] = {"error": f"{type(e).__name__}: {e}"}
                if verbose:
                    print(f"  [warn] 订阅组「{prof.name}」摘要失败: "
                          f"{type(e).__name__}: {e}")
                continue
            total["groups"][prof.slug] = stat

        # 后处理组可能刚把共享论文升级为 deep;现在再给所有成功组补说明。
        for prof in groups:
            stat = total["groups"].get(prof.slug)
            if stat is None or "error" in stat:
                continue
            try:
                _complete_group_relevance(conn, cfg, prof, ids[prof.slug], llm, stat,
                                          limit=limit, days=days, verbose=verbose, force=force)
                db.log_run(conn, "summarize", "ok", stat, started_at=started[prof.slug],
                           group_slug=prof.slug)
            except Exception as e:  # noqa: BLE001
                total["errors"] += 1
                stat["error"] = f"{type(e).__name__}: {e}"
                db.log_run(conn, "summarize", "failed", stat, error=str(e),
                           started_at=started[prof.slug], group_slug=prof.slug)
            conn.commit()
            for key in ("deep", "brief", "brief_failed", "skipped", "relevance"):
                total[key] += int(stat.get(key, 0) or 0)
        return total
    finally:
        conn.close()
