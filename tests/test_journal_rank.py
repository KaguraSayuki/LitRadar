"""期刊等级:刊名清洗 + 标签渲染规则。

这两件事绑在一起:等级是按**刊名**去查的,刊名里混着 HTML 实体或换行就查不到;
查到之后又得压成短标签,否则卡片上会被十几个体系淹没。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from litradar import db  # noqa: E402
from litradar.journal_rank import DEFAULT_FIELDS, DEFAULT_MAP, Renderer  # noqa: E402


# ------------------------------------------------------------- 刊名清洗
@pytest.mark.parametrize("raw, want", [
    ("Organic &amp; Biomolecular Chemistry", "Organic & Biomolecular Chemistry"),
    ("Journal of the American\nChemical Society", "Journal of the American Chemical Society"),
    ("The Journal\nof Organic Chemistry", "The Journal of Organic Chemistry"),
    ("  Tetrahedron   Letters  ", "Tetrahedron Letters"),
    ("Nature Synthesis", "Nature Synthesis"),
    ("", None),
    (None, None),
])
def test_刊名清洗(raw, want):
    assert db.clean_journal(raw) == want


def test_清洗在入库时生效(tmp_path):
    """必须落在 upsert 这个唯一入口上 —— 只在显示层兜底的话,
    按刊名做的功能(统计分组、期刊等级查询)照样是坏的。"""
    database = db.Database(tmp_path / "t.db")
    database.init()
    conn = database.connect()
    db.upsert_item(conn, {"kind": "paper", "dedup_key": "doi:10.1/a", "doi": "10.1/a",
                          "title": "T", "title_norm": "t",
                          "journal": "Journal of the American\nChemical Society",
                          "source": "crossref"})
    got = conn.execute("SELECT journal FROM item").fetchone()[0]
    assert got == "Journal of the American Chemical Society"


def test_老库里的脏刊名会被迁移(tmp_path):
    database = db.Database(tmp_path / "t.db")
    database.init()
    conn = database.connect()
    conn.execute("""INSERT INTO item (kind, dedup_key, title, title_norm, journal, source,
                                      created_at, updated_at)
                    VALUES ('paper','doi:10.1/b','T','t',
                            'Organic &amp; Biomolecular Chemistry','crossref','x','x')""")
    conn.execute("PRAGMA user_version = 0")      # 装成版本化之前的老库
    conn.commit()
    database._migrate(conn)
    conn.commit()
    assert conn.execute("SELECT journal FROM item").fetchone()[0] == \
        "Organic & Biomolecular Chemistry"


# ------------------------------------------------------------- 标签渲染
def _r(**kw):
    return Renderer(kw.get("fields", DEFAULT_FIELDS), kw.get("map", DEFAULT_MAP))


def test_空标签只去掉名字不去掉字段():
    """空值 ≠ 隐藏字段。用户列的 6 个字段里有一半在 map 里是空的,
    要是把空当隐藏,他自己要的字段就全没了。"""
    assert _r().tags({"sci": "Q1"}) == ["Q1"]


def test_分区缩写走正则():
    r = _r()
    assert r.tags({"sciUp": "化学1区"}) == ["化1"]
    assert r.tags({"sciUp": "生物学2区"}) == ["生2"]
    assert r.tags({"sciUp": "环境科学与生态学1区"}) == ["环1"]
    assert r.tags({"sciUp": "物理与天体物理3区"}) == ["物3"]


def test_dollar_引用不会被原样吐出():
    """$1 要翻译成 re.sub 的 \\g<1>。没翻译的话会渲染出 "化$1"。"""
    assert "$" not in "".join(_r().tags({"sciUp": "化学1区"}))


def test_字面量改写标签名():
    assert _r().tags({"pku": "是"}) == ["北核"]
    # 标签与值相同时不重复印("EI EI" 很蠢)
    assert Renderer(["eii"], {"EI检索": "EI"}).tags({"eii": "EI"}) == ["EI"]


def test_是_这类开关值只印标签():
    """'北大中文核心 是' 很蠢,值只表示'在不在这个名单里'。"""
    assert _r().tags({"pku": "是"}) == ["北核"]
    assert _r().tags({"pku": "否"}) == ["北核 否"]


def test_字段顺序与去重():
    tags = _r().tags({"sci": "Q1", "sciUp": "化学1区", "sciif": "16.6"})
    assert tags == ["Q1", "化1", "16.6"]


def test_只渲染fields里列出的字段():
    r = _r(fields=["sciif"])
    assert r.tags({"sci": "Q1", "sciUp": "化学1区", "sciif": "16.6"}) == ["16.6"]


def test_没数据的字段不产生空标签():
    assert _r().tags({}) == []
    assert _r().tags(None) == []
    assert _r().tags({"sci": "", "sciUp": None}) == []


def test_写错的正则不会让页面挂掉():
    r = _r(map={"/(unclosed/": "x", "SCI": ""})
    assert r.tags({"sci": "Q1"}) == ["Q1"]


# ------------------------------------------------------------- ISSN 兜底
def test_issn_兜底救回短刊名(tmp_path):
    """S2 只给短刊名("Angewandte Chemie"),按全名查不到;
    但同 ISSN 下别的条目查到了全名,应该能复用。"""
    database = db.Database(tmp_path / "t.db")
    database.init()
    conn = database.connect()
    db.save_journal_rank(conn, "Angewandte Chemie International Edition",
                         {"sci": "Q1", "sciUp": "化学1区"})
    db.upsert_item(conn, {"kind": "paper", "dedup_key": "doi:10.1/full", "doi": "10.1/full",
                          "title": "F", "title_norm": "f", "issn": "1433-7851",
                          "journal": "Angewandte Chemie International Edition",
                          "source": "crossref"})
    conn.commit()
    assert db.journal_ranks_by_issn(conn).get("1433-7851") == \
        {"sci": "Q1", "sciUp": "化学1区"}


def test_查不到的刊记hit0避免反复消耗额度(tmp_path):
    database = db.Database(tmp_path / "t.db")
    database.init()
    conn = database.connect()
    db.save_journal_rank(conn, "Synfacts", None)
    assert db.journal_ranks(conn) == {}
    assert conn.execute("SELECT hit FROM journal_rank").fetchone()[0] == 0
