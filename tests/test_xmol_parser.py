"""X-MOL 邮件解析器回归测试。基准样本: fixtures/xmol_sample.eml"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from litradar.sources import xmol_email  # noqa: E402

FIXTURE = ROOT / "fixtures" / "xmol_sample.eml"


@pytest.fixture(scope="module")
def records():
    assert FIXTURE.exists(), f"缺少基准样本 {FIXTURE}"
    recs, meta = xmol_email.parse_file(FIXTURE)
    return recs, meta


def test_邮件头解析(records):
    _, meta = records
    assert "X-MOL" in meta["from"]
    assert meta["subject"] == "您在X-MOL的订阅邮件"
    assert meta["message_id"]


def test_条目数量(records):
    recs, _ = records
    assert len(recs) == 2


def test_标题被正确还原(records):
    """span 切断与硬断行必须还原,不能出现 'Benzo-N , O' 或 'α -Carbonyl'。"""
    recs, _ = records
    t = recs[0].title
    assert "α-Carbonyl" in t, t
    assert "Benzo-N,O-acetals" in t, t
    assert "  " not in t, "存在多余空格"
    assert "\n" not in t


def test_DOI与期刊(records):
    recs, _ = records
    assert recs[0].doi == "10.1021/acs.orglett.6c03259"
    assert recs[0].journal == "Org. Lett."
    assert recs[0].impact_factor == 4.7
    assert recs[0].published_at == "2026-09-04"
    assert recs[1].doi == "10.1021/acs.joc.6c01762"
    assert recs[1].impact_factor == 3.3


def test_Outlook_safelinks_被拆掉(records):
    """链接必须还原成真实 x-mol 地址,而不是 safelinks 包装。"""
    recs, _ = records
    for r in recs:
        assert r.xmol_url, "未提取到 x-mol 链接"
        assert "safelinks" not in r.xmol_url
        assert r.xmol_url.startswith("https://www.x-mol.com/paper/")


def test_作者拆分(records):
    recs, _ = records
    assert recs[0].authors[0] == "Xin Li"
    assert len(recs[0].authors) == 6
    assert recs[1].authors == ["Jiabao Tian", "Yanan Wang", "JinXiong Zhang", "Lei Zhou"]


def test_高亮命中词被提取(records):
    """红色 span 是命中的订阅关键词,属于有用元数据。"""
    recs, _ = records
    assert recs[0].matched_keywords == ["Diazos"]


def test_页脚未串入条目(records):
    """末条容易把'还有更多…请移步'页脚读进来。"""
    recs, _ = records
    for r in recs:
        assert "还有更多" not in (r.title or "")
        assert "X-MOL客服" not in (r.title or "")
        assert "祝您" not in (r.authors or [])


def test_转成入库数据(records):
    recs, _ = records
    data = xmol_email.to_item_data(recs[0], source_ref="test")
    assert data["dedup_key"] == "doi:10.1021/acs.orglett.6c03259"
    assert data["source"] == "xmol"
    assert data["abstract"] is None, "邮件本身没有摘要,应由富化补齐"
