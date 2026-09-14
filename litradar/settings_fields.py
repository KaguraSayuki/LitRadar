"""Editable configuration fields, their user-facing labels and validation."""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from .config import Config
from .settings import SettingsError, get_value, lines, put_value


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = "text"
    hint: str = ""
    low: float = 0
    high: float = 100000
    choices: tuple = ()
    advanced: bool = False


SERVICES = [
    Field("sources.xmol_enabled", "采集 X-MOL 订阅邮件", "bool"),
    Field("sources.s2_search_enabled", "Semantic Scholar 文献检索", "bool", "需要接入对应的 API 密钥"),
    Field("sources.s2_enabled", "Semantic Scholar 摘要与引用数补全", "bool"),
    Field("sources.snowball_enabled", "追踪种子论文的后续引用", "bool", "在研究方向中添加种子论文"),
    Field("sources.crossref_enabled", "Crossref 元数据补全", "bool"),
    Field("sources.crossref_search_enabled", "Crossref 文献检索", "bool", "匹配较宽泛，建议用预览检查相关性"),
    Field("sources.openalex_enabled", "OpenAlex 文献检索", "bool", "需要接入 OpenAlex 密钥"),
    Field("sources.mailto", "联系邮箱", "email", "可选，用于数据服务识别请求来源"),
    Field("mail.mode", "邮件接入方式", "select", choices=(("folder", "上传邮件文件"), ("imap", "连接邮箱"), ("maildir", "现有 Maildir（由部署管理）"))),
    Field("mail.imap_host", "邮箱服务器", hint="例如 imap.gmail.com、imap.qq.com；需要邮箱提供商支持授权码登录"),
    Field("mail.imap_port", "加密邮箱端口", "int", "通常是 993", 1, 65535),
    Field("mail.imap_user", "邮箱账号"),
    Field("mail.imap_folder", "邮件文件夹", hint="通常为 INBOX"),
    Field("mail.imap_mark_seen", "处理成功后标为已读", "bool", "开启时默认只读取未读邮件；关闭时重复读取后按邮件标识去重"),
    Field("sources.s2_search_lookback_days", "Semantic Scholar 回溯范围（天）", "int", "范围越大，请求结果越多", 1, 3650, advanced=True),
    Field("sources.s2_search_year", "限定发表年份", hint="可留空，或填写 2024、2024-2026；仍按回溯天数筛选", advanced=True),
    Field("sources.s2_search_max_pages", "每条检索最多读取页数", "int", "每页最多 1000 篇，增加页数会增加耗时", 1, 20, advanced=True),
    Field("sources.s2_min_interval", "Semantic Scholar 请求间隔（秒）", "float", "默认 5 秒，减少被限流的概率", 1, 300, advanced=True),
    Field("sources.s2_venues", "Semantic Scholar 限定期刊", "lines", "每行一本；留空则不限制期刊", advanced=True),
    Field("sources.crossref_lookback_days", "Crossref 回溯范围（天）", "int", low=1, high=3650, advanced=True),
    Field("sources.crossref_rows", "Crossref 每次检索篇数", "int", low=1, high=1000, advanced=True),
    Field("sources.openalex_lookback_days", "OpenAlex 回溯范围（天）", "int", low=1, high=3650, advanced=True),
    Field("sources.openalex_per_query", "OpenAlex 每条检索篇数", "int", low=1, high=200, advanced=True),
    Field("sources.snowball_max_seeds", "最多追踪种子论文数", "int", low=1, high=1000, advanced=True),
    Field("sources.snowball_seeds_per_run", "每次刷新种子论文数", "int", low=1, high=1000, advanced=True),
    Field("sources.snowball_min_cocitations", "至少关联多少篇种子论文", "int", low=1, high=1000, advanced=True),
    Field("sources.snowball_per_seed", "每篇种子最多获取引用论文数", "int", low=1, high=1000, advanced=True),
]

MODEL = [
    Field("llm.base_url", "API 地址", "url", "填写 API 根地址，保留服务要求的 /v1 等路径。密钥会发送到这个地址，请使用可信的服务。"),
    Field("llm.model", "模型名称", hint="填写服务提供方给出的完整模型标识。"),
    Field("llm.json_mode", "JSON 输出方式", "select", "自动模式会在接口明确拒绝 JSON 参数时，改用提示词约束，并校验返回结果。", choices=(("auto", "自动适配（推荐）"), ("json_object", "接口 JSON 模式"), ("prompt", "提示词约束 JSON")), advanced=True),
    Field("llm.token_limit_parameter", "输出长度参数", "select", "自动模式先使用传统参数，接口明确拒绝时改用推理模型参数。", choices=(("auto", "自动适配（推荐）"), ("max_tokens", "max_tokens（传统接口）"), ("max_completion_tokens", "max_completion_tokens（推理模型）")), advanced=True),
    Field("llm.temperature", "生成随机程度", "optional_float", "留空使用模型默认值。不支持此参数的接口会自动省略它。", 0, 2, advanced=True),
    Field("llm.timeout", "模型请求超时（秒）", "int", low=5, high=600, advanced=True),
]

READING = [
    Field("llm.enabled", "启用 AI 排序与摘要", "bool", "关闭后保留已有摘要，新文献按关键词和规则排序"),
    Field("llm.deep_summary_top_n", "每个方向生成深度摘要的篇数", "int", "按相关度选择；其他文献生成简要摘要，同篇中性摘要在各方向复用", 0, 200),
    Field("llm.rerank_top_k", "每个方向最多精排篇数", "int", "0 表示处理范围内全部候选；会产生模型调用费用", 0, 10000),
    Field("app.pipeline_window_days", "文献处理范围（天）", "int", "排序与摘要覆盖这段时间，应不小于已启用来源的回溯范围", 1, 3650),
    Field("journal_rank.enabled", "展示期刊等级与影响因子", "bool", "需要 easyScholar 密钥"),
    Field("journal_rank.max_lookups", "每次最多查询新期刊数", "int", "结果会缓存，后续优先复用", 0, 1000),
    Field("llm.rerank_batch_size", "每个精排批次篇数", "int", "过大可能超过模型输入上限", 1, 100, advanced=True),
    Field("ranking.weights.llm", "AI 评分权重", "float", "默认 0.85；三项权重之和必须大于零", 0, 100, advanced=True),
    Field("ranking.weights.coarse", "关键词评分权重", "float", "默认 0.10", 0, 100, advanced=True),
    Field("ranking.weights.rule", "规则评分权重", "float", "默认 0.05", 0, 100, advanced=True),
    Field("admin.cooldown_seconds", "同阶段最短运行间隔（秒）", "int", "0 表示不限；适用于网页、命令行和自动更新", 0, 86400, advanced=True),
    Field("admin.daily_limit", "每个方向、每个受限阶段每天最多运行次数", "int", "0 表示不限。限制运行次数，不是货币预算。邮件与补全是全局阶段。", 0, 10000, advanced=True),
]
PAGES = {"services": SERVICES, "reading": READING}


def display_values(cfg: Config, fields: list[Field]) -> dict:
    data = asdict(cfg)
    data["ranking"]["weights"] = {key: getattr(cfg.ranking, "w_" + key) for key in ("llm", "coarse", "rule")}
    return {f.key: "\n".join(get_value(data, f.key, []) or []) if f.kind == "lines"
            else (get_value(data, f.key) if get_value(data, f.key) is not None else "") for f in fields}


def parse_fields(form, fields: list[Field], cfg: Config) -> dict:
    patch = {}
    for f in fields:
        if f.kind != "bool" and f.key not in form:
            continue
        raw = str(form.get(f.key, "")).strip()
        if f.kind == "bool":
            value = raw == "on"
        elif f.kind == "optional_float" and not raw:
            value = None
        elif f.kind in ("int", "float", "optional_float"):
            try:
                value = int(raw) if f.kind == "int" else float(raw)
                if not math.isfinite(value) or not f.low <= value <= f.high:
                    raise ValueError
            except (ValueError, OverflowError):
                raise SettingsError(f"{f.label}请填写 {f.low:g} 至 {f.high:g} 之间的{'整数' if f.kind == 'int' else '数值'}。", f.key) from None
        elif f.kind == "select":
            if raw not in dict(f.choices):
                raise SettingsError("请选择列表中的选项。", f.key)
            value = raw
        elif f.kind == "lines":
            value = lines(raw)
        else:
            value = raw
            if len(raw) > 2000 or any(ord(c) < 32 for c in raw):
                raise SettingsError("内容过长或包含换行，请检查输入。", f.key)
            if f.kind == "url":
                try:
                    u = urlsplit(raw)
                    u.port
                except ValueError:
                    raise SettingsError("API 地址格式无效，请检查主机名和端口。", f.key) from None
                if (u.scheme not in ("http", "https") or not u.hostname or u.username or u.password
                        or u.query or u.fragment or any(c.isspace() for c in raw) or '\\' in raw):
                    raise SettingsError("请填写不含账号、密码和查询参数的 HTTP 或 HTTPS 服务地址。", f.key)
                if f.key == "llm.base_url":
                    value = raw.rstrip('/').removesuffix('/chat/completions')
            if f.key == "llm.model" and not raw:
                raise SettingsError("请填写模型名称。", f.key)
            if f.kind == "email" and raw and ("@" not in raw or " " in raw):
                raise SettingsError("请填写完整的联系邮箱，或留空。", f.key)
        patch[f.key] = value
    if fields is MODEL:
        return patch  # Connection fields have no dependency on collection or reading settings.
    data = asdict(cfg)
    for key, value in patch.items():
        put_value(data, key, value)
    src = data["sources"]
    window = data["app"]["pipeline_window_days"]
    for enabled, days in (("s2_search_enabled", "s2_search_lookback_days"),
                          ("crossref_search_enabled", "crossref_lookback_days"),
                          ("openalex_enabled", "openalex_lookback_days")):
        if src[enabled] and src[days] > window:
            field = "sources." + days if fields is SERVICES else "app.pipeline_window_days"
            raise SettingsError(f"采集回溯范围不能超过文献处理范围（当前 {window} 天）。请缩小回溯天数，或先在 AI 与阅读中增大处理范围。", field)
    year = str(src["s2_search_year"] or '')
    if year and (not re.fullmatch(r"\d{4}(-\d{4})?", year)
                 or ('-' in year and year.split('-')[0] > year.split('-')[1])):
        raise SettingsError("年份请填写四位数字，或起止年份，例如 2024-2026。", "sources.s2_search_year")
    if src["snowball_seeds_per_run"] > src["snowball_max_seeds"]:
        raise SettingsError("每次刷新的种子数不能大于最多追踪种子数。", "sources.snowball_seeds_per_run")
    if data["mail"]["mode"] == "imap" and (not data["mail"]["imap_host"] or not data["mail"]["imap_user"]):
        raise SettingsError("连接邮箱时请填写服务器和邮箱账号。", "mail.imap_host")
    weights = [patch.get("ranking.weights." + k, getattr(cfg.ranking, "w_" + k)) for k in ("llm", "coarse", "rule")]
    if sum(weights) <= 0:
        raise SettingsError("三项排序权重不能同时为零。", "ranking.weights.llm")
    return patch


def validate_restored_config(cfg: Config) -> None:
    """Backups receive the same numeric and cross-field checks as web forms."""
    from .scheduler import validate
    for fields in [*PAGES.values(), MODEL]:
        values = display_values(cfg, fields)
        for field in fields:
            if field.kind == 'bool':
                if not isinstance(values[field.key], bool):
                    raise SettingsError(f"备份中的{field.label}格式不正确，请选择另一份备份。")
                values[field.key] = 'on' if values[field.key] else ''
        parse_fields(values, fields, cfg)
    validate(cfg)
