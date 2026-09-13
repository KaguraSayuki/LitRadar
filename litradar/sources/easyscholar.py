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

import requests

from ..config import read_secret

BASE = "https://www.easyscholar.cc/open/getPublicationRank"

# 接口最多只等这么久。期刊等级是锦上添花,不该拖慢富化。
TIMEOUT = 20

CODE_OK = 200
CODE_BAD_KEY = 40002
CODE_NO_NAME = 40004
CODE_NO_KEY = 40005

# config.yaml 的 journal_rank.api_key_env 可以改成别的名字,这里只是默认值。
DEFAULT_KEY_ENV = "EASYSCHOLAR_SECRET_KEY"


class RankLookupError(RuntimeError):
    """easyScholar 本次查询失败,结果不能写成永久的负缓存。

    ``fetch_rank`` 用 ``None`` 表示接口已经正常处理了刊名,但没有可用的
    等级数据。网络、认证和响应协议错误则抛出这个异常,让调用方保留下次
    查询的机会。
    """


def api_key(env_name: str = DEFAULT_KEY_ENV) -> str | None:
    """读密钥;``env_name`` 由调用方从 ``journal_rank.api_key_env`` 传入。

    以前这里写死 ``EASYSCHOLAR_SECRET_KEY``,于是 config.yaml 里那个
    ``api_key_env`` 形同虚设 —— 改名的用户会看到检查命令说"已设置",
    实际查询却拿不到密钥。去空白等语义统一在 :func:`config.read_secret`。
    """
    return read_secret(env_name or DEFAULT_KEY_ENV)


def available(env_name: str = DEFAULT_KEY_ENV) -> bool:
    return bool(api_key(env_name))


def _pick(data: dict) -> dict[str, str]:
    """从返回里挑出要展示的等级。

    优先用 officialRank.select —— 那是用户在自己的 easyScholar 控制台里
    勾选过的体系,和他在别处看到的一致;没有 select 时退回 all。
    """
    off = data.get("officialRank")
    if off is None:
        off = {}
    if not isinstance(off, dict):
        raise RankLookupError("easyScholar officialRank 字段不是对象")
    for field in ("select", "all"):
        value = off.get(field)
        if value is not None and not isinstance(value, dict):
            raise RankLookupError(f"easyScholar officialRank.{field} 字段不是对象")
    ranks = off.get("select") or off.get("all") or {}
    return {k: str(v) for k, v in ranks.items() if v not in (None, "", [])}


def fetch_rank(journal: str, env_name: str = DEFAULT_KEY_ENV) -> dict[str, str] | None:
    """按刊名查等级。返回 {字段: 值};``None`` 表示成功但没有等级记录。

    网络、认证、HTTP 或响应协议失败抛出 :class:`RankLookupError`。期刊等级
    是可选数据,调用方应捕获这个异常并跳过本次缓存,而不是把临时故障记成
    ``hit=0``。
    """
    key = api_key(env_name)
    name = (journal or "").strip()
    # 这两种情况不会发请求。调用方在进入本函数前也会检查 available() 和
    # 非空刊名,保留 None 便于直接调用者处理本地无效输入。
    if not key or not name:
        return None
    try:
        r = requests.get(BASE, params={"secretKey": key, "publicationName": name},
                         timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001  网络失败不能变成永久负缓存
        raise RankLookupError(f"easyScholar 请求失败: {type(exc).__name__}") from exc

    # easyScholar 通常用 JSON 中的 code 表示业务结果,但网关/服务端错误
    # 仍可能直接返回 HTTP 错误。先按 HTTP 语义检查,再解析业务 JSON。
    try:
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001  HTTP 错误不能变成永久负缓存
        raise RankLookupError("easyScholar HTTP 请求失败") from exc

    try:
        payload = r.json()
    except Exception as exc:  # noqa: BLE001  非 JSON 响应属于协议失败
        raise RankLookupError("easyScholar 返回的不是有效 JSON") from exc

    if not isinstance(payload, dict):
        raise RankLookupError("easyScholar JSON 顶层不是对象")

    code = payload.get("code")
    if code != CODE_OK:
        # 40002/40005 是密钥问题,其它非成功 code 也不能证明刊名不存在。
        # 40004 是本地输入无效;当前调用路径不会传空刊名,仍按协议失败处理,
        # 以免将一个拼接/清洗 bug 写成永久负缓存。
        raise RankLookupError(f"easyScholar 业务错误 code={code!r}")

    if "data" not in payload:
        raise RankLookupError("easyScholar 响应缺少 data 字段")
    data = payload["data"]
    if data is None:
        # code=200 且 data 为空是接口对该刊名的成功无结果响应。
        return None
    if not isinstance(data, dict):
        raise RankLookupError("easyScholar data 字段不是对象")
    try:
        return _pick(data) or None
    except Exception as exc:  # noqa: BLE001  字段结构变化属于协议失败
        raise RankLookupError("easyScholar 等级字段格式无效") from exc
