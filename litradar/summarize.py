"""结构化中文摘要 + 中文标题翻译。带数字核验,防止 LLM 编造实验数据。"""
from __future__ import annotations

import json
import re
import sqlite3

from . import db
from .config import Config
from .llm import DeepSeek, LLMError
from .rank import Profile, load_interests

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

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_FIELDS = ("one_liner", "problem", "method", "key_results", "limitation", "relevance")


def _numbers(text: str) -> set[str]:
    return set(_NUM_RE.findall(text or ""))


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


def summarize_one(row: sqlite3.Row, prof: Profile, llm: DeepSeek) -> dict:
    abstract = row["abstract"] or ""
    prompt = SUMMARY_PROMPT.format(
        title=row["title"], journal=row["journal"] or "未知",
        published=row["published_at"] or "未知", authors=_join_authors(row) or "未知",
        abstract=abstract[:3000] or "(无摘要,只有标题和期刊信息)",
        direction=prof.direction[:300],
    )
    data = llm.json(SUMMARY_SYSTEM, prompt, max_tokens=1400)

    out: dict = {"title_zh": (str(data.get("title_zh") or "").strip() or None)}
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


def summarize_brief(rows: list[sqlite3.Row], llm: DeepSeek) -> dict[int, dict]:
    """批量:每条产出中文标题 + 一句话结论。"""
    blocks = []
    for i, r in enumerate(rows, 1):
        blocks.append(f"[{i}] 标题: {r['title']}\n    摘要: {(r['abstract'] or '(无摘要)')[:700]}")
    try:
        data = llm.json(BRIEF_SYSTEM, BRIEF_PROMPT.format(items="\n\n".join(blocks)),
                        max_tokens=2500)
    except LLMError as e:
        print(f"  [warn] 批量摘要失败: {e}")
        return {}

    out: dict[int, dict] = {}
    entries = data.get("items") or data.get("summaries") or []
    for entry in entries:
        idx = int(entry.get("id", 0)) - 1
        if 0 <= idx < len(rows):
            out[int(rows[idx]["id"])] = {
                "title_zh": (str(entry.get("title_zh") or "").strip() or None),
                "one_liner": (str(entry.get("one_liner") or "").strip() or None),
            }
    return out


def run(cfg: Config, *, limit: int = 200, days: int = 30, verbose: bool = True,
        force: bool = False) -> dict:
    """生成中文摘要。默认覆盖窗口内**全部**条目,而不仅是前几名。"""
    prof = load_interests(cfg)
    llm = DeepSeek(cfg.llm)
    stat: dict = {"deep": 0, "brief": 0, "brief_failed": 0, "skipped": 0}
    if not llm.available:
        return {"skipped": "未配置 API key 或 LLM 已禁用"}

    database = db.Database(cfg.db_file)
    conn = database.connect()
    started = db.now()
    try:
        # 待处理 = 从没做过摘要的
        #        + 摘要做在富化之前的(那会儿还没摘要,总结质量差,现在有摘要了要重做)
        # force 时全部重做。
        if force:
            where = "1=1"
        else:
            where = """(
                su.item_id IS NULL
                OR (e.enriched_at > su.created_at
                    AND i.abstract IS NOT NULL AND i.abstract <> ''
                    AND su.depth = 'brief')
            )"""
        rows = list(conn.execute(
            f"""SELECT i.*, COALESCE(sc.final_score,-1) AS fs
                FROM item i
                LEFT JOIN summary          su ON su.item_id = i.id
                LEFT JOIN score            sc ON sc.item_id = i.id
                LEFT JOIN item_enrichment  e  ON e.item_id  = i.id
                WHERE {where} AND i.kind='paper'
                  AND COALESCE(i.published_at,'') >= date('now', ?)
                ORDER BY fs DESC, i.published_at DESC
                LIMIT ?""",
            (f"-{days} days", limit),
        ).fetchall())
        stat["pending"] = len(rows)

        deep_n = cfg.llm.deep_summary_top_n
        heads, rest = rows[:deep_n], rows[deep_n:]

        # 前 N 名:逐篇深度摘要
        for r in heads:
            if verbose:
                print(f"  深度摘要 #{r['id']}: {r['title'][:56]}…")
            try:
                data = summarize_one(r, prof, llm)
                db.save_summary(conn, int(r["id"]), data, "deep", cfg.llm.model)
                conn.commit()
                stat["deep"] += 1
            except LLMError as e:
                print(f"  [warn] 摘要失败: {e}")
                stat["skipped"] += 1

        # 其余:批量,只出中文标题 + 一句话
        bs = 10
        for s in range(0, len(rest), bs):
            chunk = rest[s:s + bs]
            if verbose:
                print(f"  批量摘要 {s + 1}-{s + len(chunk)} / {len(rest)}…")
            brief = summarize_brief(chunk, llm)
            if not brief:
                stat["brief_failed"] += len(chunk)
                continue
            for r in chunk:
                iid = int(r["id"])
                if iid in brief:
                    db.save_summary(conn, iid, brief[iid], "brief", cfg.llm.model)
                    stat["brief"] += 1
            conn.commit()

        db.log_run(conn, "summarize", "ok", stat, started_at=started)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        db.log_run(conn, "summarize", "failed", stat, error=str(e), started_at=started)
        conn.commit()
        raise
    finally:
        conn.close()
    return stat
