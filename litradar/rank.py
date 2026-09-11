"""排序:三阶段漏斗。

    Stage 1  规则过滤      命中 negative 直接丢;命中关键词/期刊/CAS → 加分
    Stage 2  BM25 粗排     把候选裁到 top-K(DeepSeek 无 embedding API,故不用向量)
    Stage 3  LLM 精排      分批 listwise 打分 + 给理由

最终分 = w_llm*llm + w_coarse*coarse + w_rule*rule
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import db
from .config import Config
from .llm import DeepSeek, LLMError

# ------------------------------------------------------------ 期刊名缩写匹配
# 首字母匹配用的停用词。
# 注意:不要把 "international" 放进来 —— 期刊缩写写作 "Int.",它不在停用词表里,
# 会造成 "Angewandte Chemie International Edition" -> 'ace' 而
# "Angew. Chem. Int. Ed." -> 'acie' 的不对称,导致匹配失败。
_STOPWORDS = {"of", "the", "and", "&", "for", "on", "a", "an"}


def journal_initials(name: str) -> str:
    """把期刊名压成首字母串,用于匹配全称与缩写。

    'Organic Letters' / 'Org. Lett.'                      -> 'ol'
    'Journal of the American Chemical Society' / 'JACS'   -> 'jacs'
    """
    tokens = re.split(r"[^A-Za-z0-9]+", name or "")
    letters = [t[0].lower() for t in tokens if t and t.lower() not in _STOPWORDS]
    return "".join(letters)


def journal_tokens(name: str) -> set[str]:
    return {t.lower() for t in re.split(r"[^A-Za-z0-9]+", name or "") if t}


def journal_matches(journal: str | None, whitelist: list[str]) -> str | None:
    """返回命中的白名单刊名(精确 / 首字母 / token 包含)。"""
    if not journal:
        return None
    j_norm = journal_initials(journal)
    j_tok = journal_tokens(journal)
    for w in whitelist:
        if journal.lower().strip() == w.lower().strip():
            return w
        if journal_initials(w) == j_norm and len(j_norm) >= 2:
            return w
        w_tok = journal_tokens(w)
        if w_tok and len(w_tok & j_tok) >= max(2, len(w_tok) - 1):
            return w
    return None


# ---------------------------------------------------------------- 兴趣偏好
@dataclass
class Profile:
    name: str
    direction: str
    core: list[str]
    bonus: list[str]
    negative: list[str]
    current_challenges: list[str]
    boost_topics: list[str]
    journals_core: list[str]
    journals_ok: list[str]
    authors_watch: list[str]
    search_query: str = ""
    search_queries: list[str] = field(default_factory=list)
    exclude_title_prefixes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        kw = d.get("keywords") or {}
        jr = d.get("journals") or {}
        multi = [q for q in (d.get("search_queries") or []) if q and q.strip()]
        return cls(
            name=d.get("name") or "default",
            direction=(d.get("direction") or "").strip(),
            core=[k.lower() for k in kw.get("core") or []],
            bonus=[k.lower() for k in kw.get("bonus") or []],
            negative=[k.lower() for k in d.get("negative") or []],
            current_challenges=kw.get("current_challenges") or [],
            boost_topics=kw.get("boost_topics") or [],
            journals_core=jr.get("core") or [],
            journals_ok=jr.get("ok") or [],
            authors_watch=d.get("authors_watch") or [],
            search_query=(d.get("search_query") or "").strip(),
            search_queries=multi,
            exclude_title_prefixes=[p.lower() for p in
                                    (d.get("exclude_title_prefixes") or [])],
        )

    @property
    def queries(self) -> list[str]:
        """检索词列表。多查询并集是为了**召回**(实测 +86%),不是为了精确率。"""
        if self.search_queries:
            return self.search_queries
        if self.search_query:
            return [self.search_query]
        return []

    @property
    def query(self) -> str:
        """给 BM25 粗排用的查询串(把多条检索词拼起来)。

        注意:这只是给 BM25 打分用的;**检索 API 必须用 queries 里的英文短句**,
        中文的 direction 描述拿去检索会一无所获。
        """
        if self.queries:
            return " ".join(self.queries)
        terms = list(self.core) + list(self.bonus[:8])
        seen, out = set(), []
        for t in terms:
            tl = t.lower()
            if tl not in seen:
                seen.add(tl)
                out.append(t)
        return " ".join(out)


def load_interests(cfg: Config) -> Profile:
    return Profile.from_dict(cfg.interests_data or {})


# ------------------------------------------------------------------- Stage 1
def haystack(row: sqlite3.Row) -> str:
    parts = [row["title"] or "", row["abstract"] or "", row["journal"] or ""]
    try:
        parts += json.loads(row["authors"] or "[]")
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        parts += json.loads(row["matched_keywords"] or "[]")
    except (json.JSONDecodeError, TypeError):
        pass
    return " ".join(parts).lower()


def rule_filter(rows: list[sqlite3.Row], prof: Profile) -> tuple[list[tuple[sqlite3.Row, float, dict]], int]:
    """返回 (保留的 (row, rule_score, detail), 被丢弃数)。"""
    kept, dropped = [], 0
    for row in rows:
        title_l = (row["title"] or "").lower()

        # 非论文记录:Crossref 会登记同行评审与补充材料,
        # 标题形如 "Review for …" / "Decision letter for …" /
        # "Cope et al. supplementary material"。按**前缀**判断比子串匹配精确,
        # 不会误伤正当标题里含 "review" 的论文。
        if any(title_l.startswith(p) for p in prof.exclude_title_prefixes):
            dropped += 1
            continue

        text = haystack(row)

        if any(neg.lower() in text for neg in prof.negative):
            dropped += 1
            continue

        score, detail = 0.0, {}
        core_hits = [k for k in prof.core if k in text]
        bonus_hits = [k for k in prof.bonus if k in text]
        score += 42 * min(len(core_hits), 3)
        score += 9 * min(len(bonus_hits), 4)
        detail["core_hits"] = core_hits
        detail["bonus_hits"] = bonus_hits

        jc = journal_matches(row["journal"], prof.journals_core)
        if jc:
            score += 26
            detail["journal_core"] = jc
        elif journal_matches(row["journal"], prof.journals_ok):
            score += 8
            detail["journal_ok"] = True

        # X-MOL 邮件里的红色高亮 = 命中的订阅关键词,是强信号
        try:
            mk = json.loads(row["matched_keywords"] or "[]")
        except (json.JSONDecodeError, TypeError):
            mk = []
        if mk:
            score += 12 * min(len(mk), 2)
            detail["xmol_matched"] = mk

        for a in prof.authors_watch:
            surname = a.split()[-1] if " " in a else a
            surname = re.sub(r"[()（）]", "", surname).strip()
            if surname and len(surname) > 2 and surname.lower() in text:
                score += 14
                detail.setdefault("author_hits", []).append(a)
                break

        row_keys = row.keys()
        if "impact_factor" in row_keys and row["impact_factor"]:
            score += min(float(row["impact_factor"]), 20.0) * 0.8

        detail["score"] = round(score, 2)
        kept.append((row, score, detail))
    return kept, dropped


# ------------------------------------------------------------------- Stage 2
def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9\u4e00-\u9fff]+", (text or "").lower()) if len(t) > 1]


def coarse_rank(kept: list[tuple[sqlite3.Row, float, dict]], prof: Profile,
                top_k: int) -> list[tuple[sqlite3.Row, float, dict]]:
    """BM25 粗排。期刊白名单命中项保送,不参与裁剪。"""
    if not kept:
        return []
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        return sorted(kept, key=lambda x: -x[1])[:top_k]

    corpus = [_tokenize(haystack(r)) for r, _, _ in kept]
    query = _tokenize(prof.query)
    if not query:
        return sorted(kept, key=lambda x: -x[1])[:top_k]

    bm25 = BM25Okapi(corpus)
    scores = bm25.get_scores(query)
    lo, hi = float(min(scores)), float(max(scores))
    span = (hi - lo) or 1.0

    scored = []
    for (row, rule, detail), s in zip(kept, scores):
        norm = (float(s) - lo) / span * 100.0
        detail["coarse"] = round(norm, 2)
        scored.append((row, rule, detail, norm))

    must = [x for x in scored if x[2].get("journal_core")]
    rest = [x for x in scored if not x[2].get("journal_core")]
    must.sort(key=lambda x: -(x[3] * 0.7 + x[1] * 0.3))
    rest.sort(key=lambda x: -(x[3] * 0.7 + x[1] * 0.3))

    # 核心期刊条目优先进入,但 top_k 是**硬上限** —— 否则 24 本期刊里有大半是
    # "核心" 时,保送逻辑会让几乎所有条目都进 LLM,rerank_top_k 形同虚设
    # (实测配置 40 却送进去 66 条)。
    picked = (must + rest)[:max(1, top_k)]
    return [(r, rule, d) for r, rule, d, _ in picked]


# ------------------------------------------------------------------- Stage 3
RERANK_SYSTEM = "你是严谨的化学文献筛选助手,只输出 JSON。"

RERANK_PROMPT = """根据用户的研究方向与偏好,为每篇候选文献打相关度分。

【用户的研究方向与偏好】
研究方向:{direction}
核心关键词:{core}
加分关键词:{bonus}
核心期刊:{journals}

【用户当前的实验卡点(能对症的论文应显著加权)】
{challenges}

【希望优先命中的主题】
{boost}

【排除方向】
{negative}

【候选文献】
{items}

【打分标准】
90-100 直接相关,方法与方向高度匹配,或能直接帮助解决上述卡点
70-89  明显相关,值得一读
40-69  沾边,可有可无
0-39   不相关

【硬性要求】
1. 只依据给定信息判断,不得推测
2. reason 用中文,不超过 30 字,说明"为什么推给这位用户"
3. 命中排除方向的,分数必须低于 30
4. 只输出 JSON,不要任何额外文字

输出格式:
{{"scores":[{{"id":1,"score":85,"reason":"光氧化还原C–H活化,与在研项目直接相关"}}]}}"""


def _format_item(i: int, row: sqlite3.Row) -> str:
    abs_ = (row["abstract"] or "")[:400]
    return (f"[{i}] {row['title']}\n"
            f"    期刊: {row['journal'] or '?'} | 日期: {row['published_at'] or '?'}"
            f" | IF: {row['impact_factor'] if 'impact_factor' in row.keys() else '?'}\n"
            f"    摘要: {abs_ or '(无摘要)'}")


def llm_rerank(rows: list[tuple[sqlite3.Row, float, dict]], prof: Profile,
               cfg: Config, llm: DeepSeek) -> dict[int, tuple[float, str]]:
    """返回 {item_id: (llm_score, reason)}。失败时返回空 dict,由调用方降级。"""
    out: dict[int, tuple[float, str]] = {}
    bs = max(5, cfg.llm.rerank_batch_size)

    for start in range(0, len(rows), bs):
        batch = rows[start:start + bs]
        listing = "\n".join(_format_item(i + 1, r) for i, (r, _, _) in enumerate(batch))
        prompt = RERANK_PROMPT.format(
            direction=prof.direction[:400],
            core=", ".join(prof.core),
            bonus=", ".join(prof.bonus[:20]),
            journals=", ".join(prof.journals_core),
            challenges="\n".join(f"- {c}" for c in prof.current_challenges) or "-",
            boost="\n".join(f"- {b}" for b in prof.boost_topics) or "-",
            negative=", ".join(prof.negative),
            items=listing,
        )
        try:
            data = llm.json(RERANK_SYSTEM, prompt, max_tokens=2048)
            for entry in data.get("scores", []):
                idx = int(entry.get("id", 0)) - 1
                if 0 <= idx < len(batch):
                    iid = int(batch[idx][0]["id"])
                    out[iid] = (float(entry.get("score", 0)),
                                str(entry.get("reason", ""))[:80])
        except (LLMError, ValueError, KeyError, TypeError) as e:
            print(f"  [warn] 精排批次 {start//bs+1} 失败: {e}")
            continue
    return out


# ------------------------------------------------------------------- driver
def run(cfg: Config, *, days: int = 30, verbose: bool = True) -> dict:
    prof = load_interests(cfg)
    database = db.Database(cfg.db_file)
    conn = database.connect()
    started = db.now()
    stat: dict[str, Any] = {}

    try:
        rows = list(conn.execute(
            """SELECT i.* FROM item i
               WHERE i.kind='paper' AND COALESCE(i.published_at,'') >= date('now', ?)
               ORDER BY i.published_at DESC""",
            (f"-{days} days",),
        ).fetchall())
        stat["candidates"] = len(rows)

        kept, dropped = rule_filter(rows, prof)
        stat["after_rule"] = len(kept)
        stat["dropped_by_rule"] = dropped

        kept = coarse_rank(kept, prof, cfg.llm.rerank_top_k)
        stat["after_coarse"] = len(kept)

        # 清掉本轮【最终未被保留】条目的旧分数。
        # 必须在 coarse_rank 之后做:放在之前只会按中间集合清理,
        # 那些"过了规则但没进 LLM"的条目会带着上一轮的分数残留下来
        # (实测:本轮只评 50 条,库里却有 66 条分数)。
        kept_ids = [int(r["id"]) for r, _, _ in kept]
        window = ("item_id IN (SELECT id FROM item WHERE kind='paper' "
                  "AND COALESCE(published_at,'') >= date('now', ?))")
        if kept_ids:
            ph = ",".join("?" for _ in kept_ids)
            conn.execute(
                f"DELETE FROM score WHERE {window} AND item_id NOT IN ({ph})",
                [f"-{days} days", *kept_ids],
            )
        else:
            conn.execute(f"DELETE FROM score WHERE {window}", [f"-{days} days"])
        conn.commit()

        llm = DeepSeek(cfg.llm)
        llm_scores: dict[int, tuple[float, str]] = {}
        if llm.available and kept:
            if verbose:
                print(f"  LLM 精排 {len(kept)} 篇…")
            llm_scores = llm_rerank(kept, prof, cfg, llm)
            stat["llm_scored"] = len(llm_scores)
        else:
            stat["llm_skipped"] = "未配置 API key 或已禁用"

        w = cfg.ranking
        for row, rule, detail in kept:
            iid = int(row["id"])
            coarse = float(detail.get("coarse", 0.0))
            rule_norm = min(rule, 100.0)
            coarse_norm = min(coarse, 100.0)
            if iid in llm_scores:
                ls, reason = llm_scores[iid]
                final = (w.w_llm * ls + w.w_coarse * coarse_norm + w.w_rule * rule_norm)
            else:
                ls, reason = None, None
                total_w = w.w_coarse + w.w_rule
                final = ((w.w_coarse * coarse_norm + w.w_rule * rule_norm) / total_w
                         if total_w else 0.0)
            db.save_score(
                conn, iid,
                rule_score=round(rule, 2), coarse_score=round(coarse, 2),
                llm_score=ls, llm_reason=reason,
                llm_model=cfg.llm.model if ls is not None else None,
                final_score=round(final, 2),
            )
        stat["scored"] = len(kept)
        db.log_run(conn, "rank", "ok", stat, started_at=started)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        db.log_run(conn, "rank", "failed", stat, error=str(e), started_at=started)
        conn.commit()
        raise
    finally:
        conn.close()
    return stat
