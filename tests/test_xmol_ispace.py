"""X-MOL 个人空间(ispace)适配器的解析与失败行为。

fixtures 全部为合成数据:DOI 用 ``10.5555/litradar.example.`` 前缀,标题以
``Synthetic`` 开头,作者以 ``Example`` 结尾,订阅词用与仓储无关的占位词。
这样测试既覆盖真实响应结构,又不会把任何人的真实研究方向带进仓库。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from litradar import db  # noqa: E402
from litradar.sources import xmol_ispace as xi  # noqa: E402


# --------------------------------------------------------------- 合成响应

def _paper(**over) -> dict:
    """一条接口记录,字段与真实响应同名同形(含 HTML 高亮与换行作者)。"""
    base = {
        "paperId": "1000000000000000001",
        "journalId": 1,
        "doi": "10.5555/litradar.example.paper-a",
        "journalName": "Synthetic Journal of Examples",
        "journalShortName": "Synth. J. Exam.",
        "isOa": True,
        "oaStatus": "gold",
        "title": "Synthetic Study of Exemplary Cascades",
        "summary": ("Synthetic abstract text describing an exemplary "
                    "<span style='color:#ff0000'>cascade</span> process "
                    "with <span style='color:#ff0000'>cyclizations</span>."),
        "summaryZh": "合成的中文摘要，用于验证中文列写入。",
        "pubDate": "2026-01-02",
        "url": "https://example.org/paper-a",
        "img": None,
        "updateDate": 1767225600000,
        "impactFactor": 7.5,
        "author": "Alpha Example \n            (示例甲), Beta Example \n            (示例乙)",
        "isRead": None,
        "userFavorite": None,
        "hasPaperNote": None,
    }
    base.update(over)
    return base


def _envelope(obj):
    return {"jsonType": "mySpaceJsonResult", "obj": obj,
            "success": True, "failCode": None, "message": None}


SUBS = {11: "cascade", 12: "[2,2] && cyclizations", 13: "Alpha Example"}

SUBSCRIPTION_TREE = _envelope([
    {"journalKeywordSubscribeGroupInfoId": 0, "name": "未分组", "keywordCount": 3,
     "children": [
         {"journalKeywordSubscribeId": 11, "keywordType": 1, "keyword": "cascade"},
         {"journalKeywordSubscribeId": 12, "keywordType": 1,
          "keyword": "[2,2] && cyclizations"},
         {"journalKeywordSubscribeId": 13, "keywordType": 2,
          "keyword": "Alpha Example"},
     ]},
])


@pytest.fixture(autouse=True)
def _clear_keyword_cache():
    """订阅树有模块级缓存,测试之间必须隔离,否则会互相漏响应。"""
    xi._KEYWORD_CACHE = (0.0, {})
    yield
    xi._KEYWORD_CACHE = (0.0, {})


# --------------------------------------------------------------- 字段转换

def test_字段映射到白名单内的键():
    data = xi.to_item_data(_paper(), SUBS)
    assert set(data) <= set(db.ITEM_FIELDS), "多出的键会被 db 静默丢弃,不该产生"


def test_高亮提取命中词并清洗摘要():
    data = xi.to_item_data(_paper(), SUBS)
    # 高亮必须在 clean_html_text 之前提取,否则 span 被抹掉就再也拿不到。
    assert "<span" not in data["abstract"]
    assert data["abstract"].startswith("Synthetic abstract text")
    # 片段要归因到**订阅词**,不是照抄高亮原文。
    assert data["matched_keywords"] == ["cascade", "[2,2] && cyclizations"]


def test_作者按换行与逗号混合分隔():
    data = xi.to_item_data(_paper(), SUBS)
    assert data["authors"] == ["Alpha Example (示例甲)", "Beta Example (示例乙)"]


def test_期刊用全称而非缩写():
    """journal_rank 缓存按刊名精确匹配,存缩写会查不到等级。"""
    data = xi.to_item_data(_paper(), SUBS)
    assert data["journal"] == "Synthetic Journal of Examples"
    assert data["journal"] != _paper()["journalShortName"]


def test_中文摘要写入独立字段不影响英文摘要():
    data = xi.to_item_data(_paper(), SUBS)
    assert data["abstract_zh"] == "合成的中文摘要，用于验证中文列写入。"
    assert "Synthetic abstract text" in data["abstract"]


def test_缺标题的记录被丢弃():
    assert xi.to_item_data(_paper(title=""), SUBS) is None
    assert xi.to_item_data(_paper(title=None), SUBS) is None


def test_缺出版商链接时不编造_XMOL_详情页当地址():
    data = xi.to_item_data(_paper(url=None), SUBS)
    assert data["url"] is None
    # xmol_url 是独立列,仍然记录 X-MOL 侧的位置。
    assert data["xmol_url"] == "https://www.x-mol.com/paper/1000000000000000001"


def test_日期与影响因子解析():
    data = xi.to_item_data(_paper(pubDate="2026-03-04", impactFactor=3), SUBS)
    assert data["published_at"] == "2026-03-04"
    assert data["impact_factor"] == 3.0
    assert xi.to_item_data(_paper(impactFactor="n/a"), SUBS)["impact_factor"] is None


# --------------------------------------------------------------- 命中词归因

def test_布尔订阅词能命中扩展片段():
    """X-MOL 做词干/子串扩展:订阅 [2,2] && cyclizations,可能只高亮片段。"""
    data = xi.to_item_data(
        _paper(summary="... <span style='color:#ff0000'>cyclization</span> ..."), SUBS)
    assert data["matched_keywords"] == ["[2,2] && cyclizations"]


def test_无法归因的片段保留原文而不是丢弃():
    data = xi.to_item_data(
        _paper(summary="... <span style='color:#ff0000'>unrelated fragment</span> ..."),
        SUBS)
    assert data["matched_keywords"] == ["unrelated fragment"]


def test_没有订阅清单时保留高亮片段():
    data = xi.to_item_data(
        _paper(summary="... <span style='color:#ff0000'>cascade</span> ..."), {})
    assert data["matched_keywords"] == ["cascade"]


@pytest.mark.parametrize("keyword,expected", [
    ("[2,2] && cyclizations", ["2,2", "cyclizations", "cyclization"]),
    ("alpha-beta rearrangement", ["alpha-beta rearrangement", "alpha", "beta",
                                  "rearrangement"]),
    ("Alpha Example", ["alpha example", "alpha", "example"]),
    ("cascade", ["cascade"]),
])
def test_订阅词拆解(keyword, expected):
    assert xi._parts(keyword) == expected


# --------------------------------------------------------------- 失败行为

class _Resp:
    def __init__(self, status: int, payload=None, content: bytes | None = None,
                 raw: bytes | None = None):
        self.status_code = status
        self._payload = payload
        if raw is not None:
            self.content = raw
        elif content is not None:
            self.content = content
        else:
            self.content = b"x"

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _json_resp(payload) -> _Resp:
    """一个带非空 body 的 200 响应 —— body 为空会被当成会话失效。"""
    return _Resp(200, payload, content=b"{}")


class _Session:
    """替代 requests.Session:按调用顺序返回预设响应,并记录请求。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params or {}))
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _papers_session(papers_payload, monkeypatch=None) -> _Session:
    """只覆盖"拉文献"这一步:订阅树走缓存,不占响应队列。"""
    xi._KEYWORD_CACHE = (9e18, dict(SUBS))     # 永不过期的缓存
    return _Session([_json_resp(papers_payload)])


def _fetch_with(responses, **kw):
    """完整两段流程:先订阅树,再文献列表。"""
    return xi.fetch("atk0210=fake; rtk0210=fake", min_interval=0,
                    session=_Session([_json_resp(SUBSCRIPTION_TREE), *responses]), **kw)


def test_401_抛出会话过期且不重试():
    xi._KEYWORD_CACHE = (9e18, dict(SUBS))
    session = _Session([_Resp(401, content=b"")])
    with pytest.raises(xi.IspaceAuthError) as err:
        xi.fetch("t", min_interval=0, session=session)
    assert "过期" in str(err.value)
    assert len(session.calls) == 1, "401 不得重试"


def test_429_抛出限流且不重试():
    xi._KEYWORD_CACHE = (9e18, dict(SUBS))
    session = _Session([_Resp(429, content=b"")])
    with pytest.raises(xi.IspaceBlockedError) as err:
        xi.fetch("t", min_interval=0, session=session)
    assert "429" in str(err.value)
    assert len(session.calls) == 1, "429 不得重试"


def test_200_空响应体按会话失效处理():
    """实测未登录时会出现 200 + 空 body,必须报会话问题而不是当成"没有结果"。"""
    xi._KEYWORD_CACHE = (9e18, dict(SUBS))
    session = _Session([_Resp(200, content=b"")])
    with pytest.raises(xi.IspaceAuthError):
        xi.fetch("t", min_interval=0, session=session)


def test_非_JSON_响应报接口改版():
    with pytest.raises(xi.IspaceError) as err:
        _fetch_with([_json_resp(SUBSCRIPTION_TREE), _Resp(200, raw=b"<html>")])
    assert "改版" in str(err.value)


def test_响应外壳校验():
    with pytest.raises(xi.IspaceError) as err:
        _fetch_with([_json_resp({"obj": [], "success": True})])
    assert "结构" in str(err.value)


def test_缺少令牌直接拒绝():
    with pytest.raises(xi.IspaceAuthError):
        xi.fetch("   ", min_interval=0, session=_Session([]))


def test_网络异常包装成可展示的错误():
    xi._KEYWORD_CACHE = (9e18, dict(SUBS))
    session = _Session([requests.ConnectionError("boom")])
    with pytest.raises(xi.IspaceError) as err:
        xi.fetch("t", min_interval=0, session=session)
    assert "连接" in str(err.value)


def test_订阅树失败降级为提示而不是中断采集():
    """订阅树只用于给命中词起名,拿不到不该让整轮采集失败。"""
    session = _Session([
        _Resp(500, {"jsonType": "mySpaceJsonResult", "obj": None}),
        _json_resp(_envelope({"pageNo": 1, "totalRecord": 1,
                              "results": [_paper()]})),
    ])
    result = xi.fetch("t", min_interval=0, session=session)
    assert len(result.items) == 1
    assert result.total == 1
    assert result.notes and "订阅关键词" in result.notes[0]


def test_默认只请求一页():
    session = _papers_session(_envelope({"pageNo": 1, "totalRecord": 0,
                                         "results": []}))
    xi.fetch("t", min_interval=0, session=session)
    assert session.calls[-1][1]["pageNo"] == 1


def test_页码可显式覆盖():
    session = _papers_session(_envelope({"pageNo": 3, "totalRecord": 0,
                                         "results": []}))
    xi.fetch("t", page=3, min_interval=0, session=session)
    assert session.calls[-1][1]["pageNo"] == 3


def test_订阅树有缓存时不再重复请求():
    xi._KEYWORD_CACHE = (9e18, dict(SUBS))
    session = _Session([_json_resp(_envelope({"pageNo": 1, "totalRecord": 0,
                                              "results": []}))])
    xi.fetch("t", min_interval=0, session=session)
    assert len(session.calls) == 1, "缓存命中时只应发一次请求"
    assert "loadKeywordsFollowTree" not in session.calls[0][0]


# --------------------------------------------------------------- 数据红线

def test_合成数据不含真实标识():
    """仓库 AGENTS.md:fixture 不得含真实研究方向、作者或标识。"""
    text = repr(_paper()) + repr(SUBS)
    assert "10.5555/litradar.example." in text
    assert "Synthetic" in text
    assert "@" not in text, "合成数据不该含任何邮箱"
    for banned in ("x-mol.com/spaceApi", "atk0210=", "rtk0210="):
        assert banned not in text, f"合成数据泄漏了真实接口细节:{banned}"


# --------------------------------------------------------------- 端到端入库

def test_条目能落库并保留中英文摘要(tmp_path):
    """适配器输出 -> pipeline._store 的完整链路,不联网。"""
    from litradar import pipeline

    conn = db.Database(tmp_path / "t.db").connect()
    data = xi.to_item_data(_paper(), SUBS)
    iid, status = pipeline._store(conn, data)
    assert status == "created"

    row = conn.execute(
        "SELECT doi, dedup_key, abstract, abstract_zh, journal, impact_factor, "
        "matched_keywords, source, source_ref FROM item WHERE id = ?", (iid,)
    ).fetchone()
    assert row["dedup_key"] == "doi:10.5555/litradar.example.paper-a"
    assert row["abstract"].startswith("Synthetic abstract text")
    assert row["abstract_zh"] == "合成的中文摘要，用于验证中文列写入。"
    assert row["journal"] == "Synthetic Journal of Examples"
    assert row["impact_factor"] == 7.5
    assert row["source"] == "ispace"
    assert row["source_ref"] == "1000000000000000001"
    assert "cascade" in row["matched_keywords"]
    conn.close()


def test_与邮件源同_DOI_合并为一条(tmp_path):
    """邮件源和新接口给同一篇文献时不该留下两行。

    真实场景:邮件把 url 也写成 X-MOL 链接,新接口给出版商原文地址 ——
    两边都进 url 列,靠 dedup_key 合并后只补空字段。
    """
    from litradar import pipeline

    conn = db.Database(tmp_path / "t.db").connect()
    mail_item = {
        "kind": "paper", "dedup_key": "doi:10.5555/litradar.example.paper-a",
        "doi": "10.5555/litradar.example.paper-a",
        "title": "Synthetic Study of Exemplary Cascades",
        "abstract": None,                     # 邮件没有摘要
        "source": "xmol", "source_ref": "<msg@example.com>",
        "xmol_url": "https://www.x-mol.com/paper/1000000000000000001",
    }
    api_item = xi.to_item_data(_paper(), SUBS)

    first, status_a = pipeline._store(conn, mail_item)
    second, status_b = pipeline._store(conn, api_item)

    assert first == second, "同 DOI 必须合并"
    assert (status_a, status_b) == ("created", "updated")
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 1
    row = conn.execute("SELECT abstract, abstract_zh, url, journal FROM item").fetchone()
    assert row["abstract"].startswith("Synthetic abstract text"), "空摘要应被补齐"
    assert row["abstract_zh"] == "合成的中文摘要，用于验证中文列写入。"
    assert row["url"] == "https://example.org/paper-a"
    conn.close()
