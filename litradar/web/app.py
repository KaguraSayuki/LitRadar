"""FastAPI 应用:局域网自用的文献雷达界面。"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from starlette.datastructures import QueryParams, URL

from .. import db, journal_rank, pipeline, rank, summarize
from ..config import Config, load_config
from ..lock import AlreadyRunning, single_instance
from ..normalize import days_ago
from ..passwords import verify_password
from ..rank import load_groups, load_interests, validate_interests

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


def require_same_origin(request: Request) -> None:
    """拒绝浏览器发来的跨源写请求 —— 口令之外的第二道闸。

    为什么需要:``/admin/run/*`` 是不带 CSRF token 的简单 POST。就算没设口令
    (默认只绑 127.0.0.1,看起来"外面进不来"),你在浏的任意网页都能往
    ``http://127.0.0.1:8090/admin/run/all`` 发一个无需预检的跨源 POST,
    照样把 DeepSeek 额度烧掉。

    浏览器在非 GET 请求上一定会带 Origin,且这个头改不了;curl / 定时脚本
    不带,所以"没有 Origin 就放行"不会挡住正常的自动化。
    """
    origin = request.headers.get("origin")
    if not origin:
        return

    # ``request.url`` already reflects the ASGI server's trusted proxy setup
    # (the deployment passes X-Forwarded-Proto from nginx).  Compare the full
    # origin tuple rather than only netloc: HTTPS and HTTP on the same host are
    # different origins, while an omitted default port is equivalent to the
    # explicit default port.
    from urllib.parse import urlsplit

    target = request.url
    try:
        got = urlsplit(origin)
        got_port = got.port
        target_port = target.port
    except ValueError:
        raise HTTPException(403, "跨源请求被拒绝")
    scheme = (got.scheme or "").lower()
    target_scheme = (target.scheme or "").lower()
    if (scheme not in ("http", "https")
            or target_scheme not in ("http", "https")
            or got.username is not None
            or got.password is not None
            or got.path or got.query or got.fragment
            or not got.hostname or not target.hostname):
        raise HTTPException(403, "跨源请求被拒绝")

    def effective_port(scheme: str, port: int | None) -> int | None:
        return port if port is not None else {"http": 80, "https": 443}.get(scheme)

    if (scheme != target_scheme
            or got.hostname.lower() != target.hostname.lower()
            or effective_port(scheme, got_port) != effective_port(
                target_scheme, target_port)):
        raise HTTPException(403, "跨源请求被拒绝")


# 阶段名 → run_log 里记的名字。"all" 展开成它实际会跑的每个阶段,这样
# "先点 rank 再点 all" 也会被冷却拦住(它确实会再跑一次 rank 花钱)。
_STAGE_LOG_NAMES: dict[str, tuple[str, ...]] = {
    "mail": ("ingest_mail",),
    "search": ("ingest_search",),
    "enrich": ("enrich",),
    "rank": ("rank",),
    "summarize": ("summarize",),
    "all": ("ingest_mail", "ingest_search", "enrich", "rank", "summarize"),
}

# 前端靠它区分"该弹密码框"与"口令(URL token)不对" —— 两者都是 401。
ADMIN_PASSWORD_HEADER = "X-Admin-Password"
ADMIN_PASSWORD_REQUIRED_HEADER = "X-Admin-Password-Required"


def require_exposure_safe(cfg: Config) -> None:
    """绑了非回环地址却没设接口口令 —— 直接拒绝手动触发,而不是只在 check 里提醒。

    ``litradar check`` 早就把这种组合标成 BAD,但那只是提醒:代码照旧放行,
    同网段任何人都能点着按钮烧额度。既然"对外必须带口令"是本项目写明的约定,
    就让它 fail closed。
    """
    if cfg.app.token or cfg.app.is_loopback:
        return
    raise HTTPException(
        403,
        f"服务绑定了非回环地址 {cfg.app.host!r} 但没有设置 {cfg.app.token_env};"
        " 为避免同网段任何人触发流水线,已拒绝手动运行。"
        " 请设置接口口令,或把 app.host 改回 127.0.0.1(对外经反向代理)。",
    )


def require_admin_password(request: Request, cfg: Config) -> None:
    """花钱阶段前的步进验证(密码 ≠ URL 里的 token)。

    与 require_token 同样是"设了就校验":没配密码就不拦 —— 但那种状态下
    ``litradar check`` 会明确告诉你花钱接口没有这道闸。
    """
    stored = cfg.app.admin_password_hash
    if not stored:
        return
    got = request.headers.get(ADMIN_PASSWORD_HEADER, "")
    if not verify_password(got, stored):
        raise HTTPException(
            401, "需要管理员密码",
            headers={ADMIN_PASSWORD_REQUIRED_HEADER: "1"},
        )


def require_stage_limits(cfg: Config, stage: str,
                         group_slug: str | None = None) -> None:
    """冷却 + 每日上限。账本用 run_log,所以 CLI 与定时任务跑的也计入。

    口令解决不了"点多少次":误点、写错的循环脚本、泄露的凭据都能反复花钱。
    这里把单日损失封顶。
    """
    limits = cfg.admin
    if not limits.cooldown_seconds and not limits.daily_limit:
        return
    names = _STAGE_LOG_NAMES.get(stage, (stage,))
    conn = db.Database(cfg.db_file).connect()
    try:
        rows = db.recent_stage_runs(conn, names, group_slug=group_slug)
    finally:
        conn.close()

    now = datetime.now(timezone.utc).astimezone()
    today = now.date()
    times = []
    per_stage: dict[str, int] = {}
    for name, raw in rows:
        try:
            when = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue          # 历史脏数据不该让闸门失效或 500
        times.append(when)
        if when.astimezone().date() == today:
            per_stage[name] = per_stage.get(name, 0) + 1
    if not times:
        return

    if limits.cooldown_seconds:
        last = max(times)
        waited = (now - last).total_seconds()
        if waited < limits.cooldown_seconds:
            retry = int(limits.cooldown_seconds - waited) + 1
            raise HTTPException(
                429,
                f"「{stage}」刚跑过,还要等 {retry} 秒(admin.cooldown_seconds="
                f"{limits.cooldown_seconds})",
                headers={"Retry-After": str(retry)},
            )

    if limits.daily_limit and per_stage:
        # ``all`` 会记下 5 个子阶段各一行,所以这里取**单个子阶段的最大次数**,
        # 不能求和 —— 求和的话跑一次 all 就变成 5 次,第二次就被自己拦下了。
        busiest, count = max(per_stage.items(), key=lambda kv: kv[1])
        if count >= limits.daily_limit:
            raise HTTPException(
                429,
                f"「{stage}」今天已经跑了 {count} 次(阶段 {busiest}),达到上限 "
                f"admin.daily_limit={limits.daily_limit};"
                " 要再跑请调大该值,或等明天",
            )



# 记住口令用的 cookie 名。这不是"登录会话",只是省得每次点链接都重带 ?k=。
COOKIE_NAME = "litradar_k"

# 记住"上次看的是哪个订阅组"。与口令 cookie 分开:它不是凭据,只是个视图偏好。
GROUP_COOKIE = "litradar_g"


def ui_groups(cfg: Config) -> list[Profile]:
    """给界面用的组列表。配置坏了不该让页面 500 —— 退回空表。"""
    try:
        return load_groups(cfg)
    except ValueError:
        return []


def active_group(request: Request, cfg: Config, *, require_known: bool = False) -> Profile | None:
    """当前查看的订阅组:``?g=slug`` > cookie > 第一个启用的组。

    写请求优先使用页面显式传入的组;旧页面可由同源 Referer 恢复组上下文。
    按组写入的调用方用 require_known 拒绝失效的显式组;编辑器仍可修复坏配置。
    """
    explicit = request.query_params.get("g")
    if explicit is None and request.method == "POST":
        try:
            ref = URL(request.headers.get("referer", ""))
            if (ref.replace(path="", query="", fragment="")
                    == request.url.replace(path="", query="", fragment="")):
                explicit = QueryParams(ref.query).get("g")
        except ValueError:
            pass
    wanted = (explicit or request.cookies.get(GROUP_COOKIE) or "").strip()
    groups = ui_groups(cfg)
    for g in groups:
        if g.slug == wanted:
            return g
    if explicit is not None and require_known:
        raise HTTPException(400, f"未知订阅组: {explicit}")
    if not groups:
        return None
    enabled = [g for g in groups if g.enabled]
    return (enabled or groups)[0]


def _group_url(url: str, slug: str | None) -> str:
    """把页面所属组固化在导航/表单 URL,保留原有筛选和锚点。"""
    return str(URL(url).include_query_params(g=slug)) if slug else url


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
    # Cookie 只记住默认视图;已有页面的导航和写请求各自携带渲染时的组。
    g = request.query_params.get("g")
    if g and request.method == "GET" and resp.status_code < 400:
        resp.set_cookie(GROUP_COOKIE, g, httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 180)
    return resp


def ctx(request: Request, **kw) -> dict:
    cfg = get_cfg()
    group = active_group(request, cfg)
    base = {
        "request": request,
        "today": date.today().isoformat(),
        "groups": ui_groups(cfg),
        "group": group,
        "group_slug": group.slug if group else None,
        "group_url": lambda url: _group_url(url, group.slug if group else None),
        # 让模板能判断"没评分"到底是没配 key,还是只是被规则过滤了
        "llm_ready": bool(cfg.llm.enabled and cfg.llm.api_key),
        "static_v": _static_version(),
        "home_label": HOME_LABEL,
    }
    base.update(kw)
    return base


def _conn():
    return db.Database(get_cfg().db_file).connect()


# --------------------------------------------------------------- 期刊等级标签
_renderer_cache: dict[tuple, Any] = {}


def _rank_renderer(cfg: Config):
    """按配置**内容**缓存渲染器。规则解析有点开销,而每个请求都要用。

    键不能用 id(cfg):_cfg_cache.clear() 之后旧 cfg 被回收,新 cfg 很可能
    分到同一个地址,于是命中的是过期的渲染规则。
    """
    jr = cfg.journal_rank
    key = (tuple(jr.fields or ()),
           tuple(sorted((str(k), str(v)) for k, v in (jr.map or {}).items())))
    r = _renderer_cache.get(key)
    if r is None:
        r = journal_rank.Renderer(cfg.journal_rank.fields, cfg.journal_rank.map)
        _renderer_cache.clear()      # 单份配置,留着旧的没意义
        _renderer_cache[key] = r
    return r


def _decorate(rows, conn):
    """给条目挂上展示层才算得出来的东西:期刊等级标签、兜底分标记。

    这些都依赖 web 的配置,不该塞进 db.get_items —— 否则 CLI / 测试也会被
    拖上 web 的配置。缓存表一次读全(几十行),不逐条查库。
    """
    cfg = get_cfg()
    llm_ready = bool(cfg.llm.enabled and cfg.llm.api_key)

    if cfg.journal_rank.enabled:
        ranks = db.journal_ranks(conn)
        # 刊名查不到时用 ISSN 兜底(短刊名 / 只给缩写的来源)
        by_issn = db.journal_ranks_by_issn(conn)
        aliases = {db.norm_journal(k): v
                   for k, v in (cfg.journal_rank.aliases or {}).items()}
        renderer = _rank_renderer(cfg)
    else:
        ranks, by_issn, aliases, renderer = {}, {}, {}, None

    out = []
    for r in rows:
        d = dict(r)
        j = d.get("journal")
        j = aliases.get(db.norm_journal(j), j)      # 短名 → 全名
        got = (ranks.get(db.norm_journal(j))
               or by_issn.get((d.get("issn") or "").strip()))
        d["rank_tags"] = renderer.tags(got) if renderer else []

        # 有分数、但这一篇没有 LLM 分:说明它的精排批次失败了,分数不是
        # LLM 判断的结果,和其他条目不可比。必须显式标出来,否则它和
        # "真的低分"在列表上长得一模一样。
        # (具体量级取决于本轮有没有别的批次成功:有成功则兜底分封顶 15,
        #  全部失败则整体归一化到 0-100 —— 所以标签只说"不是 LLM 打的",
        #  不去断言具体封顶值。)
        #
        # 只在 LLM 可用时才标:没配 key 时全场都没有 LLM 分,逐条标注是噪音
        # (页面顶部已经有一句全局说明)。
        d["score_partial"] = bool(
            llm_ready
            and d.get("final_score") is not None
            and d.get("llm_score") is None)
        out.append(d)
    return out


# --------------------------------------------------------------------- auth
@app.get("/healthz")
def healthz():
    return {"ok": True}


# -------------------------------------------------------------------- pages
# 每页条数。手机上一张卡片约 370px,25 条约 9 屏 —— 够扫一遍又不至于首屏太慢。
PER_PAGE = 25

# 首页在导航里的名字。这里只定义一次,顶栏、手机底栏、H1、标签页标题共用。
#
# 为什么不叫"收件箱":这个页面不是待处理的邮箱,而是雷达按相关度排出来的结果。
# "收件箱"暗示"一堆等你清空的东西",恰好和这个工具的用途相反 ——
# 它要回答的是"哪些值得看",而不是"还有多少没处理"。
HOME_LABEL = "雷达"


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
    g = active_group(request, get_cfg())
    conn = _conn()
    try:
        gslug = g.slug if g else None
        filt = dict(kind=kind, state=None if state == "all" else state,
                    min_score=min_score or None, group_slug=gslug)
        total_filtered = db.count_items(conn, **filt)
        pages = max(1, -(-total_filtered // PER_PAGE))     # 向上取整
        page = min(max(1, page), pages)                     # 越界就夹到有效范围
        rows = _decorate(db.get_items(conn, **filt, limit=PER_PAGE,
                                      offset=(page - 1) * PER_PAGE), conn)
        # 副标题里"共 N 篇"的分母必须跟当前页签是同一批条目,
        # 否则在"不感兴趣"页签会出现"共 59 篇…当前显示 149 篇"这种自相矛盾的读数。
        # 收藏/不感兴趣是独立清单,分母就是它们自己;
        # 未读/已读/全部共享同一个分母 —— 仍在考虑范围内的那批。
        scope = state if state in ("starred", "ignored") else None
        total_lib = db.count_items(conn, kind=kind, state=scope, group_slug=gslug)
        # 收藏夹有多少条 —— 放在标签上,不然用户不知道值不值得点进去
        # (收藏是全局的,所以这里不按组;忽略是按组的)
        starred_total = db.count_items(conn, kind=kind, state="starred",
                                       group_slug=gslug)
        ignored_total = db.count_items(conn, kind=kind, state="ignored",
                                       group_slug=gslug)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "inbox.html", ctx(
        request, items=rows, state=state, kind=kind, min_score=min_score,
        total=total_lib, total_filtered=total_filtered,
        starred_total=starred_total, ignored_total=ignored_total,
        page_no=page, pages=pages, per_page=PER_PAGE,
        qs=_page_params(state=state if state != "new" else None,
                        min_score=min_score, kind=kind, g=gslug),
        page="inbox"))


@app.get("/week", response_class=HTMLResponse)
def week(request: Request):
    require_token(request)
    g = active_group(request, get_cfg())
    conn = _conn()
    try:
        rows = _decorate(db.get_items(conn, kind="paper", since=days_ago(7),
                                      group_slug=(g.slug if g else None),
                                      limit=200), conn)
        heads, rest = list(rows[:3]), list(rows[3:])
    finally:
        conn.close()
    return templates.TemplateResponse(request, "week.html", ctx(
        request, heads=heads, rest=rest, page="week"))


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", page: int = 1):
    require_token(request)
    g = active_group(request, get_cfg())
    rows, pages, per = [], 1, PER_PAGE + 25      # 检索结果页稍多放一点
    conn = _conn()
    try:
        if q.strip():
            # FTS5 没有便宜的 COUNT,用"多取一条"判断还有没有下一页
            probe = db.search_items(conn, q, limit=per * page + 1,
                                    group_slug=(g.slug if g else None))
            total_hits = len(probe)
            pages = max(1, -(-total_hits // per))
            page = min(max(1, page), pages)
            start = (page - 1) * per
            rows = _decorate(db.search_items(conn, q, limit=per, offset=start,
                                             group_slug=(g.slug if g else None)), conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "search.html", ctx(
        request, items=rows, q=q, page_no=page, pages=pages,
        qs=_page_params(q=q, g=g.slug if g else None), page="search"))


@app.get("/item/{item_id}", response_class=HTMLResponse)
def item_detail(request: Request, item_id: int):
    require_token(request)
    g = active_group(request, get_cfg())
    conn = _conn()
    try:
        # 三处都按当前组:score / summary_group / group_state 的主键都带
        # group_id。漏掉组条件的后果不是报错,而是**同一条 item 关联出多行**,
        # fetchone() 取到哪一组的分全凭运气 —— 单组时看不出问题。
        # ignored 也读 group_state:item_state.ignored 是 v5 起的废弃列。
        gid = db.group_id(conn, g.slug if g else None) or -1
        row = conn.execute(
            """SELECT i.*, sc.final_score, sc.rule_score, sc.coarse_score, sc.llm_score,
                      sc.llm_reason, su.title_zh, su.one_liner, su.problem, su.method,
                      su.key_results, su.limitation, su.depth,
                      COALESCE(sg.relevance, su.relevance) AS relevance,
                      COALESCE(s.state,'new') state, COALESCE(s.starred,0) starred,
                      COALESCE(gs.ignored,0) ignored,
                      e.cited_by_count, e.is_oa, e.oa_url
               FROM item i
               LEFT JOIN score         sc ON sc.item_id=i.id AND sc.group_id=?
               LEFT JOIN summary       su ON su.item_id=i.id
               LEFT JOIN summary_group sg ON sg.item_id=i.id AND sg.group_id=?
               LEFT JOIN item_state    s  ON s.item_id=i.id
               LEFT JOIN group_state   gs ON gs.item_id=i.id AND gs.group_id=?
               LEFT JOIN item_enrichment e ON e.item_id=i.id
               WHERE i.id=?""",
            (gid, gid, gid, item_id),
        ).fetchone()
        # 在连接还开着的时候挂标签 —— _decorate 要读期刊缓存表
        it = _decorate([row], conn)[0] if row else None
        if it is not None:
            it["groups"] = db.item_group_ids(conn, item_id)
    finally:
        conn.close()
    if it is None:
        raise HTTPException(404, "条目不存在")
    return templates.TemplateResponse(request, "item.html", ctx(
        request, it=it, page="inbox"))


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    require_token(request)
    g = active_group(request, get_cfg())
    conn = _conn()
    try:
        s = db.stats(conn, group_slug=(g.slug if g else None))
    finally:
        conn.close()
    return templates.TemplateResponse(request, "stats.html", ctx(request, s=s, page="stats"))


def _editor_profile(cfg: Config):
    """Load a profile for the editor without hiding a broken disk file.

    Ranking and CLI callers use ``load_interests`` directly and therefore get
    a clear ``ValueError`` for malformed preferences.  The editor is the one
    place that must remain usable while repairing such a file, so it renders
    an empty profile and returns the validation details for a warning.
    """
    errors = validate_interests(cfg.interests_data, require_keys=False)
    if errors:
        return rank.Profile.from_dict({}), errors
    try:
        return load_interests(cfg), []
    except ValueError as exc:  # defensive: keep the repair page available
        return rank.Profile.from_dict({}), [str(exc)]


@app.get("/interests", response_class=HTMLResponse)
def interests_page(request: Request, saved: int = 0):
    require_token(request)
    cfg = get_cfg()
    raw = cfg.interests_file.read_text(encoding="utf-8") if cfg.interests_file.exists() else ""
    prof, existing_errors = _editor_profile(cfg)
    # Keep a syntactically valid but historically malformed file editable; the
    # warning tells the user why the page may show fewer counts and lets them
    # repair the raw YAML in one save.
    warning = ("当前配置有字段类型错误,请修复后再保存: "
               + "；".join(existing_errors)) if existing_errors else None
    return templates.TemplateResponse(request, "interests.html", ctx(
        request, raw=raw, prof=prof, saved=bool(saved), error=warning,
        page="interests"))


# interests.yaml 的大小上限。正常配置约 12KB,512KB 已经宽出几十倍 ——
# 再大就不是配置了,别让一次误贴把 YAML 解析器和备份目录一起撑爆。
MAX_INTERESTS_BYTES = 512 * 1024
# 带时间戳的备份留几份
INTERESTS_BACKUPS = 5


def _backup_stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup_interests(target: Path) -> None:
    """写前备份到同目录的 ``interests.yaml.20260912-153000.bak``,只留最近几份。

    以前只有一层 ``.bak``:连续两次坏保存,第二次会把好配置的备份也盖掉。
    """
    import shutil

    shutil.copy2(target, target.with_name(f"{target.name}.{_backup_stamp()}.bak"))
    # 文件名里的时间戳按字典序就是时间序,排完序删最早的
    for old in sorted(target.parent.glob(f"{target.name}.*.bak"))[:-INTERESTS_BACKUPS]:
        old.unlink(missing_ok=True)


@app.post("/interests")
def interests_save(request: Request, raw: str = Form(...)):
    require_token(request)
    require_same_origin(request)
    import yaml

    cfg = get_cfg()

    def fail(msg: str):
        prof, _ = _editor_profile(cfg)
        return templates.TemplateResponse(request, "interests.html", ctx(
            request, raw=raw, prof=prof, saved=False,
            error=msg, page="interests"), status_code=400)

    # 0) 大小上限,先于一切解析
    if len(raw.encode("utf-8")) > MAX_INTERESTS_BYTES:
        return fail(f"文件过大:超过 {MAX_INTERESTS_BYTES // 1024}KB,正常配置只有十几 KB")

    # 1) YAML 语法
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return fail(f"YAML 语法错误: {e}")

    # 2) 统一校验器负责旧版必填字段与 groups 格式,以及字段/元素类型。
    #    在这里再要求旧版顶层键会拒绝所有合法的多组配置。
    type_errors = validate_interests(data)
    if type_errors:
        return fail("偏好字段类型错误:" + "；".join(type_errors))

    group = active_group(request, cfg)
    # 3) 写前备份 —— 覆盖配置不可逆,必须留后路
    target = cfg.interests_file
    if target.exists():
        _backup_interests(target)

    # 4) 原子写入:先写同目录的临时文件,再 rename 换掉正式文件。
    #    直接 write_text 写到一半崩溃会留下半个文件 —— 整份配置就没了。
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    _cfg_cache.clear()               # 让下次请求重新加载
    return RedirectResponse(_group_url("/interests?saved=1", group.slug if group else None),
                            status_code=303)


# ── 旧路由兼容 ──────────────────────────────────────────────────────────────
# /profile 是早期名字。留重定向,避免旧书签/缓存页面撞 404
# (实测有人在旧页面点保存,拿到 404)。
@app.get("/profile")
def profile_redirect_get(request: Request):
    group = active_group(request, get_cfg())
    return RedirectResponse(_group_url("/interests", group.slug if group else None),
                            status_code=301)


@app.post("/profile")
async def profile_redirect_post(request: Request):
    """旧页面表单 action 指向 /profile,把提交内容转到新处理器。"""
    require_token(request)
    require_same_origin(request)
    from urllib.parse import parse_qs

    body = (await request.body()).decode("utf-8", "replace")
    raw = (parse_qs(body).get("raw") or [""])[0]
    if raw:
        return interests_save(request, raw=raw)
    group = active_group(request, get_cfg())
    return RedirectResponse(_group_url("/interests", group.slug if group else None),
                            status_code=303)


# ------------------------------------------------------------------ actions
@app.post("/item/{item_id}/action", response_class=HTMLResponse)
def item_action(request: Request, item_id: int, action: str = Form(...)):
    require_token(request)
    require_same_origin(request)
    # 在哪个组点的就记到哪个组。star 与已读/归档是全局的,但 ignore 是
    # "它与这个方向的关系",落到默认组的话在 B 组点一下会改掉 A 组的视图。
    # 刚写进配置、还没被流水线 sync 的组不会出问题:它名下的 item_group 是
    # 空的,收件箱里就没有条目,也就没有按钮可点。
    g = active_group(request, get_cfg(), require_known=True)
    gslug = g.slug if g else None
    conn = _conn()
    try:
        try:
            db.set_action(conn, item_id, action, group_slug=gslug)
        except ValueError as e:          # 白名单之外的 action
            raise HTTPException(400, str(e))
        conn.commit()
        # 回读也要同组,而且 ignored 只能从 group_state 读 ——
        # item_state.ignored 自 v5 起是废弃列(迁移时已清零,恒为 0)。
        # 读错列会让刚点完的按钮立刻弹回默认,用户以为没生效就反复点。
        gid = db.group_id(conn, gslug) or -1
        row = conn.execute(
            """SELECT i.*, COALESCE(s.state,'new') state,
                      COALESCE(s.starred,0) starred, COALESCE(gs.ignored,0) ignored
               FROM item i
               LEFT JOIN item_state  s  ON s.item_id=i.id
               LEFT JOIN group_state gs ON gs.item_id=i.id AND gs.group_id=?
               WHERE i.id=?""",
            (gid, item_id),
        ).fetchone()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "partials/actions.html",
                                      {"request": request, "it": row})


@app.post("/admin/run/{stage}")
def admin_run(request: Request, stage: str, days: int = 0):
    # 这里是唯一会真的花钱的入口(DeepSeek 额度),闸门一个都不能少:
    #   口令 → 同源 → 暴露检查 →(花钱阶段)密码 →(花钱阶段)冷却 + 每日上限。
    # 前端显式传入当前页面的 ?g=,口令 Cookie 仍随同源 fetch 自动发送。
    require_token(request)
    require_same_origin(request)
    cfg = get_cfg()
    require_exposure_safe(cfg)
    # 当前组(切换器/`?g=` 决定的那个)。记账与执行要用同一个组,否则"在 A 组
    # 点了一下"会算到所有组头上,上限自然就不准了。
    prof = active_group(request, cfg, require_known=True)
    gslug = prof.slug if prof else None
    if stage in cfg.admin.guarded_stages:
        require_admin_password(request, cfg)
        require_stage_limits(cfg, stage, gslug)
    # days=0 表示"用配置里的统一窗口"。之前这里默认 30,而抓取窗口是 180+,
    # 导致网页点"排序"只覆盖最近一个月,更早的条目永远是"未评分"。
    if days <= 0:
        days = cfg.app.pipeline_window_days
    # 和 CLI 用同一把锁(同一个文件),否则网页按钮会绕过它:定时任务正在
    # enrich 时点一下,两边互抢 Semantic Scholar 的 1 req/s 限流,表现为
    # "批量全部返回空"。双击按钮同理,会并发跑两份。
    try:
        with single_instance(cfg.db_file.parent / "litradar.lock"):
            # mail 与 enrich 是全局的(不按方向跑),不受当前组影响
            if stage == "mail":
                out = pipeline.ingest_mail(cfg)
            elif stage == "search":
                out = pipeline.ingest_keyword_search(cfg, group=prof)
            elif stage == "enrich":
                from .. import enrich
                out = enrich.run(cfg)
            elif stage == "rank":
                out = rank.run(cfg, days=days, group=prof)
            elif stage == "summarize":
                out = summarize.run(cfg, days=days, group=prof)
            elif stage == "all":
                out = pipeline.run_all(cfg, days=days, group=prof)
            else:
                raise HTTPException(400, f"未知阶段: {stage}")
    except AlreadyRunning:
        # CLI 那边跳过就完了;网页上必须把"没跑"说清楚,否则用户会一直点
        raise HTTPException(409, "另一个任务正在运行,请稍后再试")
    return JSONResponse(out)
