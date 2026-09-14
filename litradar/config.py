"""配置加载。密钥来自部署环境、私有凭据文件或兼容的 .env。"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .journal_rank import DEFAULT_FIELDS, DEFAULT_MAP

ROOT = Path(__file__).resolve().parent.parent
_file_values: dict[str, str] = {}

# 显式加载项目根目录的 .env —— 不要依赖当前工作目录,
# 否则 systemd 启动或其他目录下调用时会读不到密钥。
def load_env_file(path: Path | None = None) -> None:
    """Refresh file values without mixing them into the deployment environment."""
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover
        return
    env_path = path or (ROOT / ".env")
    global _file_values
    _file_values = {key: value for key, value in
                    (dotenv_values(env_path, interpolate=False) if env_path.exists() else {}).items()
                    if value}


load_env_file()


def _expand(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (ROOT / p)


# 只绑本机的几种写法。空字符串在 uvicorn 里等于绑全部网卡,所以不在其中。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# /admin/run/{stage} 接受的阶段,与 web/app.py 的分发一致。
_ADMIN_STAGES = frozenset({"mail", "search", "enrich", "rank", "summarize", "all"})


def read_secret(env_name: str | None) -> str | None:
    """读取密钥类环境变量:去掉首尾空白,空值一律当作"没设置"。

    所有密钥都走这一个入口,原因有两个:

      * ``.env`` 里 ``KEY=abc `` 这种尾随(或换行前)空白很常见,带着它去
        请求只会拿到"凭据无效 / 40002",却极难看出是空格造成的;
      * "已配置"的判断和真正发出去的凭据必须是同一个值。以前各处直接用
        ``os.environ.get``,只含空白的值会被判成"已设置",而下游要么发出
        一个带空格的密钥、要么(在 strip 之后)认为没配置 —— 结果是体检说
        没问题、功能却是空的。

    ``env_name`` 可以来自配置(如 ``llm.api_key_env``);为空即视为未设置。
    """
    from .credentials import current_values

    raw = current_values().get(env_name) if env_name else None
    return (raw or "").strip() or None


def _config_secret(section, env_name: str | None) -> str | None:
    values = getattr(section, "_secret_values", None)
    if values is None:
        return read_secret(env_name)
    return (values.get(env_name) or "").strip() or None


@dataclass
class MailConfig:
    """邮件接入方式:folder(本地 .eml) / maildir / imap。"""

    mode: str = "folder"                 # folder | maildir | imap
    # folder / maildir
    folder: str = "./data/inbox"
    move_processed_to: str | None = "./data/inbox/processed"
    # imap
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password_env: str = "IMAP_PASSWORD"
    imap_folder: str = "INBOX"
    imap_search: str = 'FROM "newsletter.x-mol.com"'
    imap_mark_seen: bool = True

    @property
    def imap_password(self) -> str | None:
        return _config_secret(self, self.imap_password_env)


@dataclass
class SourceConfig:
    xmol_enabled: bool = True

    # Crossref 分两个用途,开关分开:
    #   crossref_enabled        -> 富化:元数据最权威(期刊全称/ISSN/作者)
    #   crossref_search_enabled -> 检索发现
    # 实测(用户反馈):Crossref 检索打分 41 条,收藏 0 条;S2 打分 7 条,收藏 3 条。
    # 所以检索默认关闭,只留它做富化。
    crossref_enabled: bool = True
    crossref_search_enabled: bool = False
    crossref_lookback_days: int = 14
    crossref_rows: int = 100

    # Semantic Scholar:摘要主力。免费。
    # 官方限流 1 请求/秒(跨所有端点),这里默认放到 5s 一次更稳妥 ——
    # batch 端点一次吃 100 个 DOI,放慢几乎不增加总耗时。
    s2_enabled: bool = True
    s2_min_interval: float = 5.0
    # S2 bulk 检索:精确 AND,召回低于 Crossref 但准确率高,作为互补的第三条腿
    s2_search_enabled: bool = True
    # bulk 端点**没有日期粒度**,只有 year。留空则按 s2_search_lookback_days
    # 自动推算出年份区间。不设的话会拉回 1981 年至今的全部文献 ——
    # 实测 121 篇里只有 1 篇在 60 天窗口内,其余永远不参与排序,纯属死重量。
    s2_search_year: str = ""          # 显式指定则优先,如 "2024-2026"
    s2_search_lookback_days: int = 180     # 与 run --days 保持一致
    s2_search_max_pages: int = 1      # 每页 1000 条,一般 1 页足够
    # 传给 bulk 的 venue 过滤(逗号分隔)。实测多刊必须用逗号,用 | 会返回 0。
    s2_venues: list[str] = field(default_factory=list)
    # 引用滚雪球(前向:谁引用了种子)。每个种子 1 次请求。
    # 种子取自 interests.yaml 的 seed_dois。
    snowball_enabled: bool = True
    snowball_max_seeds: int = 25      # 总共用多少个种子(取自 interests.yaml)
    # 每轮刷新几个种子。**故意小**:S2 突发很容易 429,一轮连打十几个会失败一半,
    # 而共被引计数一旦丢种子就会静默漏判。轮着刷新反而更稳、覆盖也不差。
    snowball_seeds_per_run: int = 5
    # 至少被几个**不同种子**引用才收。默认 1 = 全收。
    #
    # 一开始我把它当精度闸门设成 2,实测太狠:严格日期过滤后,15 个种子
    # 全查一遍总共才 ~40 条候选(老论文在 200 天窗口内被引 0-13 次),
    # 而门槛 2 挡掉的 29 条里有 18 条标题明显对口。既然量这么小,
    # 精确率交给 LLM 精排就够了 —— 共被引改成**质量标注**(source_ref 里的
    # cocite:N),让人一眼看出这条是被几个种子共同引用的。
    # 种子规模涨到几百篇之后再考虑调高。
    snowball_min_cocitations: int = 1
    snowball_per_seed: int = 100      # 单个种子最多取多少条引用方

    # OpenAlex:2026 年起改为 API Key + 额度制,不配 key 会 "Insufficient budget",
    # 因此默认关闭;配上 OPENALEX_API_KEY 才启用。
    openalex_enabled: bool = False
    openalex_api_key_env: str = "OPENALEX_API_KEY"
    openalex_per_query: int = 60
    openalex_lookback_days: int = 14

    mailto: str = ""                     # 放进 Crossref polite pool


@dataclass
class LLMConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0.2
    rerank_batch_size: int = 20
    # 进 LLM 精排的条数上限。**0 = 不截断**(默认)。
    # 候选池只有 ~200 条,省这点调用微不足道;而硬截断会让 BM25 有"一票否决权",
    # 实测粗排 53/96/108 名的三篇被丢掉后永远是"未评分"。粗排只该给顺序。
    rerank_top_k: int = 0
    deep_summary_top_n: int = 8
    timeout: int = 120
    enabled: bool = True

    @property
    def api_key(self) -> str | None:
        return _config_secret(self, self.api_key_env)


@dataclass
class RankingConfig:
    # LLM 必须占主导。实测教训:原 0.6/0.25/0.15 时,一篇 LLM 只给 35 分
    # (理由写明"与研究方向不同")的论文,靠 BM25/规则的高分冲到了第 1 名 ——
    # 聪明的信号被笨的信号淹没了。BM25 与规则只负责"召回",不负责"判断"。
    w_llm: float = 0.85
    w_coarse: float = 0.10
    w_rule: float = 0.05
    coarse_method: str = "bm25"


@dataclass
class AppConfig:
    # 默认只绑回环。以前代码默认是 0.0.0.0:8080,与 config.example.yaml、deploy/
    # 和 README 全都对不上 —— 没写 config.yaml 就直接跑的用户会在不知情的情况下
    # 把所有页面(含手动触发接口)暴露给同网段。
    host: str = "127.0.0.1"
    port: int = 8090
    db_path: str = "./data/litradar.db"
    timezone: str = "Asia/Shanghai"
    # 可选脚本接口口令；普通网页用户可用访问密码会话。
    token_env: str = "LITRADAR_TOKEN"
    # 访问密码只存 PBKDF2 哈希，由首次设置或本机命令写入私有凭据文件。
    admin_password_env: str = "LITRADAR_ADMIN_PASSWORD_HASH"
    interests: str = "./interests.yaml"   # 研究方向与检索偏好

    # 流水线时间窗(天)。**必须 ≥ 抓取窗口** —— 否则抓回来的文献进了库,
    # 却因为落在排序窗口之外而永远拿不到分数,在收件箱里长成一片"未评分"。
    # 网页上的"排序/摘要"按钮也读这个值:之前按钮写死 30 天而抓取是 180 天,
    # 实测收件箱 51 条里有 26 条因此从来没进过排序器。
    pipeline_window_days: int = 200

    @property
    def token(self) -> str | None:
        return _config_secret(self, self.token_env)

    @property
    def admin_password_hash(self) -> str | None:
        return _config_secret(self, self.admin_password_env)

    @property
    def is_loopback(self) -> bool:
        """是否只绑本机。绑非回环地址又没设 token 时,/admin/run/* 会 fail closed。"""
        return (self.host or "").strip().lower() in _LOOPBACK_HOSTS


@dataclass
class AdminConfig:
    """花钱阶段(默认 rank / summarize / all)的护栏。

    口令只解决"谁能点",解决不了"点多少次":误点、脚本写错循环、凭据泄露都能
    反复花钱。所以再加冷却与每日上限,把单日损失封顶。账本用 run_log,因此
    CLI 与定时任务跑过的同样计入 —— 上限是"这一天这个阶段一共跑了几次",
    而不是"网页上点了几次"。
    """

    # 需要密码、并受冷却与每日上限约束的阶段。
    # mail / search 只花免费额度,默认不管;enrich 走 S2 / easyScholar 的免费
    # 额度(有限流),需要时自己加进来。
    guarded_stages: list[str] = field(
        default_factory=lambda: ["rank", "summarize", "all"])
    # 同一阶段两次运行的最小间隔(秒);0 = 不限制。
    cooldown_seconds: int = 60
    # 同一阶段每天最多跑几次;0 = 不限制。
    daily_limit: int = 3


@dataclass
class JournalRankConfig:
    """期刊等级(easyScholar)。影响因子/分区是付费专有数据,Crossref 与
    Semantic Scholar 都不提供 —— 之前只有 X-MOL 邮件里的条目才有 IF,
    全库 209 条只有 2 条,卡片上看着像随机出现。"""

    enabled: bool = True
    api_key_env: str = "EASYSCHOLAR_SECRET_KEY"
    # 只展示这些字段。接口一口气返回十几个体系(还有各高校自己的分级),
    # 全铺在卡片上会把真正重要的信息淹掉。
    fields: list[str] = field(default_factory=lambda: list(DEFAULT_FIELDS))
    # 字段名 → 短标签(空 = 不印名字);"/正则/" 键作用于值。见 journal_rank.py
    map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MAP))
    # 一次富化最多查几本新刊。开放接口按次计额,给个上限免得被异常数据打爆。
    max_lookups: int = 40
    # 刊名别名:来源给的短名/罗马字名 → easyScholar 认的全名。
    # 例:"Youji huaxue" 是 S2 对《有机化学》的罗马字写法;
    #    "Angewandte Chemie" 在 S2 里常指国际版(有 ISSN 时优先走 ISSN 兜底)。
    aliases: dict[str, str] = field(default_factory=dict)


@dataclass
class ScheduleConfig:
    enabled: bool = False
    time: str = "07:00"
    groups: list[str] = field(default_factory=list)  # empty = all enabled directions
    owner: str = "external"
    handoff_confirmed: bool = False


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    mail: MailConfig = field(default_factory=MailConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    sources: SourceConfig = field(default_factory=SourceConfig)
    ranking: RankingConfig = field(default_factory=RankingConfig)
    journal_rank: JournalRankConfig = field(default_factory=JournalRankConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    interests_data: dict[str, Any] = field(default_factory=dict)
    config_file: Path | None = field(default=None, repr=False, compare=False)

    # 解析后的绝对路径
    @property
    def db_file(self) -> Path:
        return _expand(self.app.db_path)

    @property
    def interests_file(self) -> Path:
        return _expand(self.app.interests)

    @property
    def inbox_dir(self) -> Path:
        return _expand(self.mail.folder)


def _build(cls, data: dict | None):
    data = data or {}
    valid = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in data.items() if k in valid})


def _ranking_config(data: dict | None) -> RankingConfig:
    """读取公开的 weights 映射,同时兼容早期平铺的 w_* 字段。"""
    if data is None:
        return RankingConfig()
    if not isinstance(data, dict):
        raise ValueError("ranking 必须是 YAML 映射")
    valid = set(RankingConfig.__dataclass_fields__) | {"weights"}
    unknown = set(data) - valid
    if unknown:
        raise ValueError(f"未知 ranking 配置项: {', '.join(sorted(map(str, unknown)))}")

    weights = data.get("weights")
    if weights is None:
        weights = {}
    if not isinstance(weights, dict):
        raise ValueError("ranking.weights 必须是包含 llm/coarse/rule 的映射")
    unknown = set(weights) - {"llm", "coarse", "rule"}
    if unknown:
        raise ValueError(f"未知 ranking.weights 配置项: {', '.join(sorted(map(str, unknown)))}")

    values = {k: v for k, v in data.items() if k != "weights"}
    for key, value in weights.items():
        field_name = f"w_{key}"
        if field_name in values and values[field_name] != value:
            raise ValueError(f"ranking.{field_name} 与 ranking.weights.{key} 冲突")
        values[field_name] = value
    cfg = RankingConfig(**values)
    for key in ("llm", "coarse", "rule"):
        value = getattr(cfg, f"w_{key}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"ranking.weights.{key} 必须是非负有限数值")
        try:
            value = float(value)
        except OverflowError as e:
            raise ValueError(f"ranking.weights.{key} 必须是非负有限数值") from e
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"ranking.weights.{key} 必须是非负有限数值")
        setattr(cfg, f"w_{key}", value)
    total = cfg.w_llm + cfg.w_coarse + cfg.w_rule
    if not math.isfinite(total) or total <= 0:
        raise ValueError("ranking.weights 总和必须是大于 0 的有限数值")
    return cfg


def _admin_config(data: dict | None) -> AdminConfig:
    """花钱阶段的护栏配置。阶段名写错会静默失去保护,所以这里严格校验。"""
    if data is not None and not isinstance(data, dict):
        raise ValueError("admin 必须是 YAML 映射")
    data = data or {}
    unknown = set(data) - set(AdminConfig.__dataclass_fields__)
    if unknown:
        raise ValueError(f"未知 admin 配置项: {', '.join(sorted(map(str, unknown)))}")
    cfg = AdminConfig(**data)

    if not isinstance(cfg.guarded_stages, (list, tuple)) \
            or not all(isinstance(s, str) for s in cfg.guarded_stages):
        raise ValueError("admin.guarded_stages 必须是阶段名列表")
    cfg.guarded_stages = [s.strip().lower() for s in cfg.guarded_stages]
    # 允许留空(等于关掉这层保护),但不允许拼错 —— 拼错的后果是"以为有护栏,
    # 其实没有",比不做更危险。
    misspelled = [s for s in cfg.guarded_stages if s not in _ADMIN_STAGES]
    if misspelled:
        raise ValueError(
            f"未知 admin.guarded_stages: {', '.join(misspelled)};"
            f" 可选 {', '.join(sorted(_ADMIN_STAGES))}")

    for name in ("cooldown_seconds", "daily_limit"):
        value = getattr(cfg, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"admin.{name} 必须是非负整数(0 = 不限制)")
    return cfg


def load_config(path: str | Path | None = None) -> Config:
    """从 config.yaml 加载;文件不存在时全部走默认值。"""
    from .lock import single_instance
    cfg_path = _expand(path or os.environ.get("LITRADAR_CONFIG", "config.yaml"))
    # Share the short-lived writer lock so a job cannot combine files from
    # different settings saves. Credentials are a separate atomic snapshot.
    with single_instance(cfg_path.with_name(cfg_path.name + '.settings.lock'), blocking=True):
        raw: dict[str, Any] = {}
        if cfg_path.exists():
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        cfg = config_from_dict(raw)
        cfg.config_file = cfg_path
        pf = cfg.interests_file
        if pf.exists():
            cfg.interests_data = yaml.safe_load(pf.read_text(encoding="utf-8")) or {}
    from .credentials import attach_snapshot
    attach_snapshot(cfg)
    return cfg


def config_from_dict(raw: dict[str, Any]) -> Config:
    """Build a configuration without reading or writing a user's files."""
    if not isinstance(raw, dict):
        raise ValueError("配置必须是映射")
    return Config(
        app=_build(AppConfig, raw.get("app")),
        llm=_build(LLMConfig, raw.get("llm")),
        mail=_build(MailConfig, raw.get("mail")),
        admin=_admin_config(raw.get("admin")),
        sources=_build(SourceConfig, raw.get("sources")),
        ranking=_ranking_config(raw.get("ranking")),
        journal_rank=_build(JournalRankConfig, raw.get("journal_rank")),
        schedule=_build(ScheduleConfig, raw.get("schedule")),
    )
