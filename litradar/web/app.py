"""FastAPI 应用:局域网自用的文献雷达界面。"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .. import db, pipeline, rank, summarize
from ..config import Config, load_config
from ..normalize import days_ago
from ..rank import load_interests

HERE = Path(__file__).resolve().parent

app = FastAPI(title="LitRadar", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _fromjson(value: Any) -> list:
    """模板里把 JSON 字符串列字段还原成 list。解析失败返回空列表,不炸页面。"""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        import json as _json

        try:
            got = _json.loads(value)
            return got if isinstance(got, list) else []
        except _json.JSONDecodeError:
            return []
    return []


templates.env.filters["fromjson"] = _fromjson


def _fromjsonobj(value: Any) -> dict:
    """run_log.stats 这类 JSON 对象字段 -> dict。解析失败返回空 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        import json as _json

        try:
            got = _json.loads(value)
            return got if isinstance(got, dict) else {}
        except _json.JSONDecodeError:
            return {}
    return {}


templates.env.filters["fromjsonobj"] = _fromjsonobj

# Crossref 的标题常带 <i>N</i>、<sub>2</sub>、<sup>+</sup> 这类排版标签(化学命名需要),
# 直接转义会把 "<i>" 原样显示出来。只放行这几个安全的行内标签,其余照常转义。
_RICH_TAGS = ("i", "b", "sub", "sup", "em", "strong")
_RICH_RE = re.compile(r"</?(%s)>" % "|".join(_RICH_TAGS))
_SCP_RE = re.compile(r"</?scp>")           # <scp> 小型大写:无害,直接去掉


def _unesc(value: Any) -> str:
    """Crossref 元数据里常见 ``&amp;``、``&#x2019;`` 这类实体,先还原成字符再交给自动转义。"""
    import html as _html

    return _html.unescape(str(value)) if value else ""


templates.env.filters["unesc"] = _unesc


def _richtitle(value: Any) -> Markup:
    from markupsafe import escape

    text = _SCP_RE.sub("", _unesc(value))
    out, pos = [], 0
    for m in _RICH_RE.finditer(text):
        out.append(escape(text[pos:m.start()]))
        out.append(Markup(m.group(0)))
        pos = m.end()
    out.append(escape(text[pos:]))
    return Markup("").join(out)


templates.env.filters["richtitle"] = _richtitle


def _static_version() -> str:
    """静态文件的缓存指纹:取 style.css / app.js 的最新修改时间。
    改了样式不必手动清缓存,手机浏览器尤其顽固。"""
    try:
        return str(int(max(p.stat().st_mtime for p in (HERE / "static").glob("*"))))
    except ValueError:
        return "0"

CONFIG_PATH = os.environ.get("LITRADAR_CONFIG")

# 预加载 config(单用户,配置很少变;改动后重启即可)
_cfg_cache: dict[str, Any] = {}


def get_cfg() -> Config:
    """加载配置。顺带重读 .env,让新填的 API key 无需重启服务即可生效。

    用 load_env_file() 而不是 load_dotenv(override=True):后者会让 .env
    覆盖真实环境变量,把 systemd / 命令行传入的配置清掉(实测导致鉴权失效)。
    """
    from ..config import load_env_file

    load_env_file()

    if "cfg" not in _cfg_cache:
        _cfg_cache["cfg"] = load_config(CONFIG_PATH)
    return _cfg_cache["cfg"]


def require_token(request: Request) -> None:
    """极简口令校验 —— 没有账号、没有会话、没有登录页。

    - 未设 LITRADAR_TOKEN:不校验(默认只绑 127.0.0.1,本就访问不到)
    - 设了   :所有请求需带 ?k=<token> 或 X-Token 头

    为什么还要留这个:``/admin/run/*`` 会真的消耗你的 DeepSeek 额度。
    如果绑 0.0.0.0 又完全不设防,同网段任何人都能把钱烧掉。
    """
    cfg = get_cfg()
    expected = cfg.app.token
    if not expected:
        return
    got = (request.query_params.get("k")
           or request.headers.get("X-Token", "")
           or request.cookies.get(COOKIE_NAME, ""))
    if not hmac.compare_digest(got, expected):
        raise HTTPException(
            status_code=401,
            detail="缺少或错误的口令:请在 URL 后加 ?k=<token>",
        )


# 记住口令用的 cookie 名。这不是"登录会话",只是省得每次点链接都重带 ?k=。
COOKIE_NAME = "litradar_k"


@app.middleware("http")
async def remember_token(request: Request, call_next):
    """用 ?k= 访问过一次后写入 cookie。

    否则页面内的链接(``/item/5``)不带 token,一点就 401 —— 那样这个口令
    根本没法用。cookie 里存的就是 token 本身,服务端不保存任何会话状态。
    """
    resp = await call_next(request)
    k = request.query_params.get("k")
    expected = get_cfg().app.token
    if k and expected and hmac.compare_digest(k, expected):
        resp.set_cookie(COOKIE_NAME, k, httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 180)
    return resp


def ctx(request: Request, **kw) -> dict:
    cfg = get_cfg()
    base = {
        "request": request,
        "today": date.today().isoformat(),
        # 让模板能判断"没评分"到底是没配 key,还是只是被规则过滤了
        "llm_ready": bool(cfg.llm.enabled and cfg.llm.api_key),
        "static_v": _static_version(),
    }
    base.update(kw)
    return base


def _conn():
    return db.Database(get_cfg().db_file).connect()


# --------------------------------------------------------------------- auth
@app.get("/healthz")
def healthz():
    return {"ok": True}


# -------------------------------------------------------------------- pages
# 每页条数。手机上一张卡片约 370px,25 条约 9 屏 —— 够扫一遍又不至于首屏太慢。
PER_PAGE = 25


def _page_params(**kw) -> str:
    """把筛选条件拼成查询串(不含 page),供分页链接复用。"""
    from urllib.parse import urlencode

    clean = {}
    for k, v in kw.items():
        if v in (None, "", 0, 0.0):
            continue
        # 20.0 -> "20",别在 URL 里留下没意义的小数点
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        clean[k] = v
    return urlencode(clean)


@app.get("/", response_class=HTMLResponse)
def inbox(request: Request, state: str = "new", kind: str = "paper",
          min_score: float = 0.0, page: int = 1):
    require_token(request)
    conn = _conn()
    try:
        filt = dict(kind=kind, state=None if state == "all" else state,
                    min_score=min_score or None)
        total_filtered = db.count_items(conn, **filt)
        pages = max(1, -(-total_filtered // PER_PAGE))     # 向上取整
        page = min(max(1, page), pages)                     # 越界就夹到有效范围
        rows = db.get_items(conn, **filt, limit=PER_PAGE,
                            offset=(page - 1) * PER_PAGE)
        total_lib = conn.execute("SELECT COUNT(*) FROM item").fetchone()[0]
    finally:
        conn.close()
    return templates.TemplateResponse(request, "inbox.html", ctx(
        request, items=rows, state=state, kind=kind, min_score=min_score,
        total=total_lib, total_filtered=total_filtered,
        page_no=page, pages=pages, per_page=PER_PAGE,
        qs=_page_params(state=state if state != "new" else None,
                        min_score=min_score),
        page="inbox"))


@app.get("/week", response_class=HTMLResponse)
def week(request: Request):
    require_token(request)
    conn = _conn()
    try:
        rows = db.get_items(conn, kind="paper", since=days_ago(7), limit=200)
        heads, rest = list(rows[:3]), list(rows[3:])
    finally:
        conn.close()
    return templates.TemplateResponse(request, "week.html", ctx(
        request, heads=heads, rest=rest, page="week"))


@app.get("/patents", response_class=HTMLResponse)
def patents(request: Request):
    require_token(request)
    conn = _conn()
    try:
        rows = db.get_items(conn, kind="patent", state=None, limit=200)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "patents.html", ctx(
        request, items=rows, page="patents"))


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", page: int = 1):
    require_token(request)
    rows, pages, per = [], 1, PER_PAGE + 25      # 检索结果页稍多放一点
    conn = _conn()
    try:
        if q.strip():
            # FTS5 没有便宜的 COUNT,用"多取一条"判断还有没有下一页
            probe = db.search_items(conn, q, limit=per * page + 1)
            total_hits = len(probe)
            pages = max(1, -(-total_hits // per))
            page = min(max(1, page), pages)
            start = (page - 1) * per
            rows = db.search_items(conn, q, limit=per, offset=start)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "search.html", ctx(
        request, items=rows, q=q, page_no=page, pages=pages,
        qs=_page_params(q=q), page="search"))


@app.get("/item/{item_id}", response_class=HTMLResponse)
def item_detail(request: Request, item_id: int):
    require_token(request)
    conn = _conn()
    try:
        row = conn.execute(
            """SELECT i.*, sc.final_score, sc.rule_score, sc.coarse_score, sc.llm_score,
                      sc.llm_reason, su.title_zh, su.one_liner, su.problem, su.method,
                      su.key_results, su.limitation, su.relevance, su.depth,
                      COALESCE(s.state,'new') state, COALESCE(s.starred,0) starred,
                      COALESCE(s.ignored,0) ignored,
                      e.cited_by_count, e.is_oa, e.oa_url
               FROM item i
               LEFT JOIN score          sc ON sc.item_id=i.id
               LEFT JOIN summary        su ON su.item_id=i.id
               LEFT JOIN item_state     s  ON s.item_id=i.id
               LEFT JOIN item_enrichment e ON e.item_id=i.id
               WHERE i.id=?""",
            (item_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "条目不存在")
    return templates.TemplateResponse(request, "item.html", ctx(request, it=row, page="inbox"))


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    require_token(request)
    conn = _conn()
    try:
        s = db.stats(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "stats.html", ctx(request, s=s, page="stats"))


@app.get("/interests", response_class=HTMLResponse)
def interests_page(request: Request, saved: int = 0):
    require_token(request)
    cfg = get_cfg()
    raw = cfg.interests_file.read_text(encoding="utf-8") if cfg.interests_file.exists() else ""
    prof = load_interests(cfg)
    return templates.TemplateResponse(request, "interests.html", ctx(
        request, raw=raw, prof=prof, saved=bool(saved), page="interests"))


@app.post("/interests")
def interests_save(request: Request, raw: str = Form(...)):
    require_token(request)
    import shutil

    import yaml

    cfg = get_cfg()

    def fail(msg: str):
        return templates.TemplateResponse(request, "interests.html", ctx(
            request, raw=raw, prof=load_interests(cfg), saved=False,
            error=msg, page="interests"), status_code=400)

    # 1) YAML 语法
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return fail(f"YAML 语法错误: {e}")

    # 2) 结构校验 —— 光"语法合法"远远不够。
    #    实测教训:提交一段合法但极小的 YAML(如 `search_queries:\n  - a`)会通过
    #    语法检查、把整份配置清空(实测把 7.9KB 的配置写成 22 字节)。
    #    所以必须确认它是 mapping 且含预期键。
    if not isinstance(data, dict):
        return fail("内容必须是一个 YAML 映射(顶层是 key: value),不能是列表或标量")
    missing = {"direction", "search_queries", "keywords", "journals"} - set(data)
    if missing:
        return fail(f"缺少必要字段:{'、'.join(sorted(missing))}。"
                    "若确实要清空某项,请保留该键并把值留空,不要提交不完整的文件。")

    # 3) 写前备份 —— 覆盖配置不可逆,必须留后路
    target = cfg.interests_file
    if target.exists():
        shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))

    target.write_text(raw, encoding="utf-8")
    _cfg_cache.clear()               # 让下次请求重新加载
    return RedirectResponse("/interests?saved=1", status_code=303)


# ── 旧路由兼容 ──────────────────────────────────────────────────────────────
# /profile 是早期名字。留重定向,避免旧书签/缓存页面撞 404
# (实测有人在旧页面点保存,拿到 404)。
@app.get("/profile")
def profile_redirect_get():
    return RedirectResponse("/interests", status_code=301)


@app.post("/profile")
async def profile_redirect_post(request: Request):
    """旧页面表单 action 指向 /profile,把提交内容转到新处理器。"""
    from urllib.parse import parse_qs

    body = (await request.body()).decode("utf-8", "replace")
    raw = (parse_qs(body).get("raw") or [""])[0]
    if raw:
        return interests_save(request, raw=raw)
    return RedirectResponse("/interests", status_code=303)


# ------------------------------------------------------------------ actions
@app.post("/item/{item_id}/action", response_class=HTMLResponse)
def item_action(request: Request, item_id: int, action: str = Form(...)):
    require_token(request)
    conn = _conn()
    try:
        db.set_action(conn, item_id, action)
        conn.commit()
        row = conn.execute(
            """SELECT i.*, COALESCE(s.state,'new') state,
                      COALESCE(s.starred,0) starred, COALESCE(s.ignored,0) ignored
               FROM item i LEFT JOIN item_state s ON s.item_id=i.id WHERE i.id=?""",
            (item_id,),
        ).fetchone()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "partials/actions.html",
                                      {"request": request, "it": row})


@app.post("/admin/run/{stage}")
def admin_run(stage: str, days: int = 30):
    cfg = get_cfg()
    if stage == "mail":
        out = pipeline.ingest_mail(cfg)
    elif stage == "search":
        out = pipeline.ingest_keyword_search(cfg)
    elif stage == "enrich":
        from .. import enrich
        out = enrich.run(cfg)
    elif stage == "rank":
        out = rank.run(cfg, days=days)
    elif stage == "summarize":
        out = summarize.run(cfg, days=days)
    elif stage == "all":
        out = pipeline.run_all(cfg, days=days)
    else:
        raise HTTPException(400, f"未知阶段: {stage}")
    return JSONResponse(out)
