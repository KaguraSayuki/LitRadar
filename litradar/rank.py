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

# ``interests.yaml`` is user-editable, so a syntactically valid YAML document
# is not necessarily a usable profile.  Keep the schema here, next to the
# consumer, so the web editor and the loader agree on the supported fields.
_INTERESTS_REQUIRED = frozenset({"direction", "search_queries", "keywords", "journals"})
_INTERESTS_SCALAR_FIELDS = ("name", "direction", "search_query")
_INTERESTS_LIST_FIELDS = (
    "search_queries", "s2_queries", "s2_venue_queries", "negative",
    "negative_titles", "exclude_title_prefixes", "authors_watch", "seed_dois",
)
_KEYWORD_LIST_FIELDS = ("core", "bonus", "current_challenges", "boost_topics")
_JOURNAL_LIST_FIELDS = ("core", "ok")


def _type_label(value: Any) -> str:
    """Return a short, stable type name for validation messages."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "布尔值"
    if isinstance(value, (int, float)):
        return "数字"
    if isinstance(value, str):
        return "字符串"
    if isinstance(value, list):
        return "列表"
    if isinstance(value, dict):
        return "映射"
    return type(value).__name__


def validate_interests(data: Any, *, require_keys: bool = True) -> list[str]:
    """Validate the structure consumed from ``interests.yaml``.

    ``None`` is deliberately accepted for every optional value and for the two
    nested mappings.  Existing files commonly use ``key:`` as an intentional
    empty value, and ``Profile.from_dict`` already treats that as empty.  List
    elements must still be strings because the ranking code calls ``strip`` or
    ``lower`` on them.  Unknown keys are retained for forward compatibility;
    all fields currently read by the application are checked here.

    The returned list is suitable for showing in the editor.  An empty list
    means that the document is safe for ``Profile.from_dict`` and the other
    preference consumers.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return [f"顶层必须是映射,当前是{_type_label(data)}"]

    if require_keys:
        missing = _INTERESTS_REQUIRED - set(data)
        if missing:
            errors.append("缺少必要字段:" + "、".join(sorted(missing)))

    def check_scalar(path: str) -> None:
        value = data.get(path)
        if value is not None and not isinstance(value, str):
            errors.append(f"{path} 必须是字符串或 null,当前是{_type_label(value)}")

    def check_string_list(path: str, value: Any) -> None:
        if value is None:
            return
        if not isinstance(value, list):
            errors.append(f"{path} 必须是字符串列表或 null,当前是{_type_label(value)}")
            return
        for i, element in enumerate(value):
            if not isinstance(element, str):
                errors.append(
                    f"{path}[{i}] 必须是字符串,当前是{_type_label(element)}")

    for path in _INTERESTS_SCALAR_FIELDS:
        check_scalar(path)
    for path in _INTERESTS_LIST_FIELDS:
        check_string_list(path, data.get(path))

    keywords = data.get("keywords")
    if keywords is not None and not isinstance(keywords, dict):
        errors.append(f"keywords 必须是映射或 null,当前是{_type_label(keywords)}")
    elif isinstance(keywords, dict):
        for path in _KEYWORD_LIST_FIELDS:
            check_string_list(f"keywords.{path}", keywords.get(path))

    journals = data.get("journals")
    if journals is not None and not isinstance(journals, dict):
        errors.append(f"journals 必须是映射或 null,当前是{_type_label(journals)}")
    elif isinstance(journals, dict):
        for path in _JOURNAL_LIST_FIELDS:
            check_string_list(f"journals.{path}", journals.get(path))
        issn = journals.get("issn")
        if issn is not None and not isinstance(issn, dict):
            errors.append(f"journals.issn 必须是映射或 null,当前是{_type_label(issn)}")
        elif isinstance(issn, dict):
            for name, values in issn.items():
                if not isinstance(name, str):
                    errors.append(
                        f"journals.issn 的键必须是字符串,当前是{_type_label(name)}")
                    continue
                check_string_list(f"journals.issn.{name}", values)

    return errors


@dataclass
class Profile:
    name: str
    direction: str
    core: list[str]
    bonus: list[str]
    negative: list[str]
    # 只匹配【标题】的排除词。用于通用领域噪声词 —— 它们常出现在
    # 正当论文的摘要里(如"compared to hydrogenation"),放 negative 会误杀。
    negative_titles: list[str]
    current_challenges: list[str]
    boost_topics: list[str]
    journals_core: list[str]
    journals_ok: list[str]
    authors_watch: list[str]
    search_query: str = ""
    search_queries: list[str] = field(default_factory=list)
    # Semantic Scholar bulk 端点的检索词。**必须用查询语法**(+ = AND,| = OR,
    # "..." = 短语),裸词会被当短语匹配而返回 0 条。
    s2_queries: list[str] = field(default_factory=list)
    # 限定在核心期刊内的宽查询 —— 用来替代 Crossref 的期刊定向覆盖
    s2_venue_queries: list[str] = field(default_factory=list)
    # 引用滚雪球的种子 DOI。必须是 1-3 年前的文献 —— 新论文被引 0-1 次,滚不出来。
    seed_dois: list[str] = field(default_factory=list)
    exclude_title_prefixes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        errors = validate_interests(d, require_keys=False)
        if errors:
            raise ValueError("偏好字段类型错误:" + "；".join(errors))

        # New web saves are validated above.  The normal field handling below
        # intentionally stays strict so CLI/ranking never silently drops a
        # malformed negative/keyword or other preference.
        kw = d.get("keywords") or {}
        jr = d.get("journals") or {}
        multi = [q for q in (d.get("search_queries") or []) if q and q.strip()]
        return cls(
            name=d.get("name") or "default",
            direction=(d.get("direction") or "").strip(),
            core=[k.lower() for k in kw.get("core") or []],
            bonus=[k.lower() for k in kw.get("bonus") or []],
            negative=[k.lower() for k in d.get("negative") or []],
            negative_titles=[k.lower() for k in d.get("negative_titles") or []],
            current_challenges=kw.get("current_challenges") or [],
            boost_topics=kw.get("boost_topics") or [],
            journals_core=jr.get("core") or [],
            journals_ok=jr.get("ok") or [],
            authors_watch=d.get("authors_watch") or [],
            search_query=(d.get("search_query") or "").strip(),
            search_queries=multi,
            s2_queries=[q for q in (d.get("s2_queries") or []) if q and q.strip()],
            s2_venue_queries=[q for q in (d.get("s2_venue_queries") or []) if q and q.strip()],
            seed_dois=[x.strip() for x in (d.get("seed_dois") or []) if x and x.strip()],
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

        # 标题专属排除词:只扫标题,不扫摘要
        if any(t in title_l for t in prof.negative_titles):
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
                top_k: int = 0) -> list[tuple[sqlite3.Row, float, dict]]:
    """BM25 粗排。只负责给出**顺序**,不再做硬截断。

    ``top_k <= 0``(默认)表示全部保留。历史上这里卡 50 条,理由是省 LLM 调用;
    但候选池只有 ~200 条,省下的调用微不足道,代价却是 BM25 误杀 ——
    实测粗排名次 53 / 96 / 108 的三篇被直接丢掉,永远拿不到 LLM 判断,
    在收件箱里长成一片"未评分"。粗排的信号强度本来就远低于 LLM,
    让它有"一票否决权"是本末倒置。

    现在粗排只做两件事:给 LLM 一个先后顺序;在核心期刊条目上做保送。
    进不进视野由 LLM 分数和界面阈值决定。
    """

    def _cap(rows: list) -> list:
        return rows[:max(1, top_k)] if top_k and top_k > 0 else rows

    if not kept:
        return []
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        return _cap(sorted(kept, key=lambda x: -x[1]))

    corpus = [_tokenize(haystack(r)) for r, _, _ in kept]
    query = _tokenize(prof.query)
    if not query:
        return _cap(sorted(kept, key=lambda x: -x[1]))

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

    # 核心期刊条目优先,但不设硬上限 —— 上限会把"核心期刊占了 24 本里一大半"
    # 这种配置下的保送放大成事实上的全部放行,反而掩盖问题。顺序由这里决定,
    # 是否值得看由 LLM 分数决定。
    picked = must + rest
    return _cap([(r, rule, d) for r, rule, d, _ in picked])


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

【用户的历史反馈(用于校准你的判断标准)】
✅ 这些是他收藏过的,代表"对口"的样子:
{liked}
❌ 这些是他点过"不感兴趣"的,代表"不要"的样子:
{disliked}
请据此校准:与 ✅ 同类的大胆给高分,与 ❌ 同类的压低分数。

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


def feedback_examples(conn: sqlite3.Connection, limit: int = 6) -> tuple[list[str], list[str]]:
    """取收藏 / 忽略的标题,供精排 prompt 做少样本校准。

    这是反馈闭环真正被消费的地方 —— 之前反馈只落库,从不影响打分。

    忽略样本只取**有分数的**:那些是 LLM 认为相关、用户却否掉的,才是有信息量的
    负例。被规则过滤的条目用户根本没看见,拿来做负例会污染判断。
    """
    liked = [r[0] for r in conn.execute(
        """SELECT i.title FROM item_state s
           JOIN item i ON i.id = s.item_id
           WHERE s.starred = 1 AND i.kind = 'paper'
           ORDER BY s.item_id DESC LIMIT ?""", (limit,))]

    disliked = [r[0] for r in conn.execute(
        """SELECT i.title FROM item_state s
           JOIN item i ON i.id = s.item_id
           JOIN score  sc ON sc.item_id = i.id
           WHERE s.ignored = 1 AND i.kind = 'paper'
           ORDER BY sc.final_score DESC LIMIT ?""", (limit,))]
    return liked, disliked


def llm_rerank(rows: list[tuple[sqlite3.Row, float, dict]], prof: Profile,
               cfg: Config, llm: DeepSeek,
               liked: list[str] | None = None,
               disliked: list[str] | None = None) -> dict[int, tuple[float, str]]:
    """返回 {item_id: (llm_score, reason)}。失败时返回空 dict,由调用方降级。"""
    out: dict[int, tuple[float, str]] = {}
    liked_txt = "\n".join(f"- {t[:90]}" for t in (liked or [])) or "(暂无)"
    disliked_txt = "\n".join(f"- {t[:90]}" for t in (disliked or [])) or "(暂无)"
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
            liked=liked_txt,
            disliked=disliked_txt,
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
            f"""SELECT i.* FROM item i
               WHERE i.kind='paper' AND {db.in_window('i')}
               ORDER BY i.published_at DESC""",
            # in_window 占两个 ?(没有 published_at 时回退比 created_at)
            (f"-{days} days", f"-{days} days"),
        ).fetchall())
        stat["candidates"] = len(rows)

        kept, dropped = rule_filter(rows, prof)
        stat["after_rule"] = len(kept)
        stat["dropped_by_rule"] = dropped

        # 被规则丢掉的条目标记为 excluded —— 收件箱据此隐藏它们。
        # 之前它们只是"没有分数",仍排在列表末尾逼用户手动忽略 ——
        # 实测 139 条忽略反馈里 119 条属于这种。
        #
        # 例外只有一个:被**收藏**的不参与自动排除。收藏是用户明确说过的
        # "我要留着",不能被后续收紧的检索词悄悄吃掉。
        starred = db.starred_ids(conn)
        kept_ids_now = {int(r["id"]) for r, _, _ in kept}
        auto_dropped = [int(r["id"]) for r in rows if int(r["id"]) not in kept_ids_now]
        dropped_ids = [i for i in auto_dropped if i not in starred]
        # 反过来说,历史上被旧逻辑误伤的收藏条目要恢复出来
        db.set_excluded(conn, [i for i in auto_dropped if i in starred], False)
        stat["protected_by_star"] = len(auto_dropped) - len(dropped_ids)
        db.set_excluded(conn, dropped_ids, True)
        conn.commit()

        db.set_excluded(conn, [int(r["id"]) for r, _, _ in kept], False)

        kept = coarse_rank(kept, prof, cfg.llm.rerank_top_k)
        stat["after_coarse"] = len(kept)

        # 清掉本轮【最终未被保留】条目的旧分数。
        # 必须在 coarse_rank 之后做:放在之前只会按中间集合清理,
        # 那些"过了规则但没进 LLM"的条目会带着上一轮的分数残留下来。粗排截断
        # 还是硬上限时踩过:本轮只评 50 条,库里却有 66 条分数。
        kept_ids = [int(r["id"]) for r, _, _ in kept]
        window = ("item_id IN (SELECT id FROM item WHERE kind='paper' "
                  f"AND {db.in_window('')})")
        if kept_ids:
            ph = ",".join("?" for _ in kept_ids)
            conn.execute(
                f"DELETE FROM score WHERE {window} AND item_id NOT IN ({ph})",
                [f"-{days} days", f"-{days} days", *kept_ids],
            )
        else:
            conn.execute(f"DELETE FROM score WHERE {window}",
                         [f"-{days} days", f"-{days} days"])
        conn.commit()

        llm = DeepSeek(cfg.llm)
        llm_scores: dict[int, tuple[float, str]] = {}
        if llm.available and kept:
            # 反馈闭环的消费端:收藏 / 否决过的标题当少样本塞进精排 prompt。
            # 之前这里没传,prompt 里的正负例永远是"(暂无)"——
            # 用户点的每一次收藏都只是落库,从不影响下一轮打分。
            liked, disliked = feedback_examples(conn)
            stat["feedback_liked"] = len(liked)
            stat["feedback_disliked"] = len(disliked)
            if verbose:
                print(f"  LLM 精排 {len(kept)} 篇…"
                      f"(参考 {len(liked)} 正例 / {len(disliked)} 负例)")
            llm_scores = llm_rerank(kept, prof, cfg, llm,
                                    liked=liked, disliked=disliked)
            stat["llm_scored"] = len(llm_scores)
        else:
            stat["llm_skipped"] = "未配置 API key 或已禁用"

        w = cfg.ranking
        # 没拿到 LLM 分的条目怎么合成 final,取决于这一轮 LLM 到底跑没跑:
        #   * 跑了、只是个别批次失败(llm_scores 非空)—— **不归一化**,分数
        #     封顶在 w_coarse+w_rule(=15)。以前这里除以 0.15 补回满量程,
        #     一个只有粗排分的条目能冲到 100,反超真被 LLM 评过的,而界面上
        #     根本看不出它没被评过。宁可让它明显偏低,也不要假装它很相关。
        #   * 压根没跑(没配 key / 全部批次失败)—— 保持归一化。此时全场都没有
        #     LLM 分,谁也不会反超谁;分数铺满 0-100,界面的阈值筛选才有意义。
        normalize_missing = not llm_scores
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
                base = w.w_coarse * coarse_norm + w.w_rule * rule_norm
                total_w = w.w_coarse + w.w_rule
                final = (base / total_w if total_w else 0.0) if normalize_missing else base
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
