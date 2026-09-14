"""Explicit, bounded connectivity checks; no ingestion or mailbox mutations."""
from __future__ import annotations

import copy
from datetime import date, timedelta

import requests

from . import credentials, db
from .config import read_secret
from .settings import SettingsError
from .sources import mail, semanticscholar


def request_json(url: str, **kwargs) -> dict:
    try:
        response = requests.get(url, timeout=25, allow_redirects=False, **kwargs)
        if response.status_code in (401, 403):
            raise SettingsError("服务拒绝了凭据。请检查密钥或账号权限后重试。")
        if response.status_code == 429:
            raise SettingsError("服务请求过于频繁，请稍后重试。")
        if response.status_code != 200:
            raise SettingsError(f"服务返回 HTTP {response.status_code}，请检查服务地址或稍后重试。")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError
        return data
    except SettingsError:
        raise
    except (requests.RequestException, ValueError):
        # URLs and provider bodies may contain credentials: never echo them.
        raise SettingsError("连接失败，请检查网络、服务地址和凭据后重试。") from None


def check(cfg, service: str) -> str:
    with credentials.snapshot(cfg):
        if service == "mail":
            if cfg.mail.mode != "imap":
                return "当前使用本地邮件。可选择邮件文件导入，或切换为连接邮箱。"
            if not cfg.mail.imap_user or not cfg.mail.imap_host or not cfg.mail.imap_password:
                raise SettingsError("请先保存邮箱服务器、账号和授权码。")
            conn = None
            try:
                conn = mail.open_imap(cfg.mail)
                if conn.login(cfg.mail.imap_user, cfg.mail.imap_password)[0] != "OK":
                    raise ValueError
                if conn.select(cfg.mail.imap_folder, readonly=True)[0] != "OK":
                    raise ValueError
                status, data = conn.uid("SEARCH", None, *mail.imap_criteria(cfg.mail))
                if status != "OK":
                    raise ValueError
                count = len((data[0] or b"").split())
                return f"连接成功，符合条件的邮件有 {count} 封。未修改邮箱状态。"
            except Exception:
                raise SettingsError("邮箱连接失败。请检查服务器、文件夹、账号及应用授权码；当前不支持仅限 OAuth2 的邮箱。") from None
            finally:
                if conn is not None:
                    try:
                        conn.logout()
                    except Exception:
                        pass
        if service == "llm":
            from .llm import DeepSeek
            if not cfg.llm.api_key:
                raise SettingsError("请先保存模型服务密钥。")
            try:
                test = copy.deepcopy(cfg.llm)
                test.timeout = min(test.timeout, 25)
                DeepSeek(test).json("Reply with JSON only.", 'Return {"ok":true}', max_tokens=16)
                return "模型调用成功。本次发送了一条简短测试消息。"
            except Exception:
                raise SettingsError("模型调用失败，请检查密钥、模型名称、服务地址及可用额度。") from None
        names = credentials.services(cfg)
        if service not in names and service != "crossref":
            raise SettingsError("未知数据服务。")
        key = read_secret(names[service][1]) if service in names else None
        if service != "crossref" and not key:
            raise SettingsError("尚未接入此服务，请先保存密钥。")
        if service == "s2":
            semanticscholar._throttle(cfg.sources.s2_min_interval)
            request_json(semanticscholar.BASE + "/paper/search", params={"query": "chemistry", "limit": 1, "fields": "title"}, headers={"x-api-key": key})
        elif service == "openalex":
            request_json("https://api.openalex.org/works", params={"per-page": 1, "api_key": key})
        elif service == "journal":
            from .sources.easyscholar import BASE
            data = request_json(BASE, params={"secretKey": key, "publicationName": "Nature"})
            if data.get("code") != 200:
                raise SettingsError("期刊服务未接受请求，请检查密钥、权限和可用额度。")
        else:
            request_json("https://api.crossref.org/works", params={"rows": 1})
        return "连接成功。测试没有采集文献。"


def preview(cfg, patch: dict) -> list[dict]:
    """At most five source queries, with explicit failure/zero-result feedback."""
    tasks = [(source, query) for source in ("s2_queries", "s2_venue_queries", "search_queries")
             for query in patch.get(source, [])]
    if not tasks:
        raise SettingsError("请先添加至少一条检索条件。", "queries")
    results = []
    with credentials.snapshot(cfg):
        for source, query in tasks[:5]:
            if source != "search_queries":
                if not cfg.sources.s2_search_enabled:
                    raise SettingsError("请先在数据与邮箱中启用 Semantic Scholar 检索。")
                key = read_secret("S2_API_KEY")
                if not key:
                    raise SettingsError("请先接入 Semantic Scholar，或选择已启用的 Crossref / OpenAlex。")
                since = (date.today() - timedelta(days=cfg.sources.s2_search_lookback_days)).isoformat()
                params = {"query": query, "fields": "title,year,url,publicationDate,externalIds",
                          "year": cfg.sources.s2_search_year or f"{since[:4]}-{date.today().year}",
                          "sort": "publicationDate:desc"}
                if source == "s2_venue_queries" and cfg.sources.s2_venues:
                    params["venue"] = ",".join(cfg.sources.s2_venues)
                semanticscholar._throttle(cfg.sources.s2_min_interval)
                data = request_json(semanticscholar.BASE + "/paper/search/bulk", params=params,
                                    headers={"x-api-key": key})
                for item in data.get("data", []):
                    if item.get("publicationDate") and item["publicationDate"] < since:
                        continue
                    results.append({"title": item.get("title", ""), "year": item.get("year"),
                                    "url": db._clean_url(item.get("url"))})
                    if len(results) >= 20:
                        return results
            else:
                if cfg.sources.crossref_search_enabled:
                    since = (date.today() - timedelta(days=cfg.sources.crossref_lookback_days)).isoformat()
                    data = request_json("https://api.crossref.org/works", params={"query": query, "rows": 5,
                        "filter": f"type:journal-article,from-pub-date:{since}"})
                    for item in data.get("message", {}).get("items", []):
                        results.append({"title": (item.get("title") or [""])[0], "url": db._clean_url(item.get("URL"))})
                if cfg.sources.openalex_enabled:
                    key = read_secret(cfg.sources.openalex_api_key_env)
                    if not key:
                        raise SettingsError("OpenAlex 尚未接入，请先保存密钥。")
                    since = (date.today() - timedelta(days=cfg.sources.openalex_lookback_days)).isoformat()
                    data = request_json("https://api.openalex.org/works", params={"search": query, "per-page": 5,
                        "filter": f"from_publication_date:{since}", "api_key": key})
                    for item in data.get("results", []):
                        results.append({"title": item.get("display_name", ""), "url": db._clean_url(item.get("doi"))})
                if not cfg.sources.crossref_search_enabled and not cfg.sources.openalex_enabled:
                    raise SettingsError("请先在数据与邮箱中启用 Crossref 或 OpenAlex 检索。")
    return results[:20]
