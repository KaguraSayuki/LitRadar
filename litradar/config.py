"""配置加载。所有密钥只从环境变量 / .env 读,不写进 config.yaml。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

# 显式加载项目根目录的 .env —— 不要依赖当前工作目录,
# 否则 systemd 启动或其他目录下调用时会读不到密钥。
def load_env_file(path: Path | None = None) -> None:
    """把 .env 里【有值且当前环境没有】的变量补进 os.environ。

    为什么不用 load_dotenv():
      * ``override=True`` 会让 .env 覆盖真实环境变量 —— 实测踩过:
        命令行/systemd 传入的 LITRADAR_TOKEN 被 .env 里的空占位符清掉,
        导致鉴权静默失效(所有请求都放行)。
      * ``override=False`` 又让"改了 .env 不重启"失效,因为首次导入时
        已经把空字符串写进了 os.environ。

    所以显式实现:**真实环境优先,空值不回填,只补缺失的**。
    """
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover
        return
    env_path = path or (ROOT / ".env")
    if not env_path.exists():
        return
    for key, value in (dotenv_values(env_path) or {}).items():
        if value and not os.environ.get(key):
            os.environ[key] = value


load_env_file()


def _expand(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (ROOT / p)


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
        return os.environ.get(self.imap_password_env)


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
    snowball_enabled: bool = True
    snowball_max_seeds: int = 10

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
        return os.environ.get(self.api_key_env)


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
    host: str = "0.0.0.0"
    port: int = 8080
    db_path: str = "./data/litradar.db"
    timezone: str = "Asia/Shanghai"
    # 没有账号体系。设了 token 就校验,没设就不校验(默认只绑 127.0.0.1)。
    token_env: str = "LITRADAR_TOKEN"
    interests: str = "./interests.yaml"   # 我的检索词与偏好(不是"账号")

    # 流水线时间窗(天)。**必须 ≥ 抓取窗口** —— 否则抓回来的文献进了库,
    # 却因为落在排序窗口之外而永远拿不到分数,在收件箱里长成一片"未评分"。
    # 网页上的"排序/摘要"按钮也读这个值:之前按钮写死 30 天而抓取是 180 天,
    # 实测收件箱 51 条里有 26 条因此从来没进过排序器。
    pipeline_window_days: int = 200

    @property
    def token(self) -> str | None:
        return os.environ.get(self.token_env)


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    mail: MailConfig = field(default_factory=MailConfig)
    sources: SourceConfig = field(default_factory=SourceConfig)
    ranking: RankingConfig = field(default_factory=RankingConfig)
    interests_data: dict[str, Any] = field(default_factory=dict)

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


def load_config(path: str | Path | None = None) -> Config:
    """从 config.yaml 加载;文件不存在时全部走默认值。"""
    cfg_path = _expand(path or os.environ.get("LITRADAR_CONFIG", "config.yaml"))
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    cfg = Config(
        app=_build(AppConfig, raw.get("app")),
        llm=_build(LLMConfig, raw.get("llm")),
        mail=_build(MailConfig, raw.get("mail")),
        sources=_build(SourceConfig, raw.get("sources")),
        ranking=_build(RankingConfig, raw.get("ranking")),
    )

    pf = cfg.interests_file
    if pf.exists():
        cfg.interests_data = yaml.safe_load(pf.read_text(encoding="utf-8")) or {}
    return cfg
