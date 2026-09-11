"""easyScholar 开放接口 —— 期刊等级 / 影响因子 / 中科院分区。

为什么用它:影响因子和分区是**付费专有数据**(Clarivate JCR / 中科院文献情报中心),
Crossref 和 Semantic Scholar 都不提供。之前 LitRadar 的 IF 只能从 X-MOL 邮件里
蹭到 —— 全库 209 条只有 2 条有,卡片上看着像随机出现。easyScholar 把各家数据
聚合起来并提供开放接口,按刊名查一次就能覆盖全库。

接口(2026 实测):
    GET https://www.easyscholar.cc/open/getPublicationRank
        ?secretKey=<你的密钥>&publicationName=<期刊全名>
    返回 {"code":200,"msg":"SUCCESS","data":{...}}
    错误码:40002 密钥错误 / 40004 文献名不能为空 / 40005 密钥不能为空

    data.officialRank.all    该账号能看到的所有等级体系
    data.officialRank.select 用户在自己控制台里勾选的那部分(更贴近他实际关心的)
    data.customRank.rank     自定义数据集

字段含义(值示例):
    sci "Q1"              JCR 分区
    sciif "16.6"          影响因子
    sciif5 "16.1"         五年影响因子
    sciUp "化学1区"        中科院升级版分区
    sciBase "化学1区"      中科院基础版分区
    sciwarn "高"           中科院预警
    pku / cssci / cscd    中文核心 / 南大核心 / CSCD
    eii "EI"              EI 检索
    jci / esi / abdc ...  其他体系

**额度**:开放接口按次计额。所以调用方务必走 `journal_rank` 缓存表 ——
全库只有 ~36 本刊,查一遍之后不再消耗。
"""
from __future__ import annotations

import os

import requests

BASE = "https://www.easyscholar.cc/open/getPublicationRank"

# 接口最多只等这么久。期刊等级是锦上添花,不该拖慢富化。
TIMEOUT = 20

CODE_OK = 200
CODE_BAD_KEY = 40002
CODE_NO_NAME = 40004
CODE_NO_KEY = 40005


def api_key() -> str | None:
    return os.environ.get("EASYSCHOLAR_SECRET_KEY") or None


def available() -> bool:
    return bool(api_key())


def _pick(data: dict) -> dict[str, str]:
    """从返回里挑出要展示的等级。

    优先用 officialRank.select —— 那是用户在自己的 easyScholar 控制台里
    勾选过的体系,和他在别处看到的一致;没有 select 时退回 all。
    """
    off = (data.get("officialRank") or {})
    ranks = off.get("select") or off.get("all") or {}
    return {k: str(v) for k, v in ranks.items() if v not in (None, "", [])}


def fetch_rank(journal: str) -> dict[str, str] | None:
    """按刊名查等级。返回 {字段: 值};``None`` 表示查不到或调用失败。

    失败一律不抛异常:期刊等级缺失不该让整条富化流水线断掉。
    """
    key = api_key()
    name = (journal or "").strip()
    if not key or not name:
        return None
    try:
        r = requests.get(BASE, params={"secretKey": key, "publicationName": name},
                         timeout=TIMEOUT)
        payload = r.json()
    except Exception:  # noqa: BLE001  网络/解析问题都当作"这次没查到"
        return None

    if payload.get("code") != CODE_OK:
        return None
    return _pick(payload.get("data") or {}) or None
