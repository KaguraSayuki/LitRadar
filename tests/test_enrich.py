"""富化测试:Crossref 的规范字段必须真的覆盖旧值(不联网)。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from litradar import db, enrich  # noqa: E402
from litradar.config import Config  # noqa: E402


# --------------------------------------------------------------- _merge
def test_crossref的规范字段进覆盖类():
    res = enrich._merge(
        "10.1/x",
        cr={"journal": "Organic Letters", "issn": "1523-7060",
            "authors": ["A", "B"], "abstract": "cr 摘要"},
        s2={"journal": "Org. Lett.", "abstract": "s2 摘要"},
        oa=None,
    )
    assert res["overwrite"] == {"journal": "Organic Letters", "issn": "1523-7060",
                                "authors": ["A", "B"]}
    # 摘要永远只填空;S2 的缩写刊名被 Crossref 顶掉,不会在 SET 里出现两次
    assert res["patch"] == {"abstract": "s2 摘要"}


def test_只有S2时刊名仍只填空():
    res = enrich._merge("10.1/x", cr=None, s2={"journal": "Org. Lett."}, oa=None)
    assert res["overwrite"] == {}
    assert res["patch"] == {"journal": "Org. Lett."}


def test_写入前洗刊名():
    """Crossref 的 container-title 带 HTML 实体和换行,以前只有入库口洗。"""
    res = enrich._merge(
        "10.1/x",
        cr={"journal": "Journal of the American\nChemical &amp; Society"},
        s2=None, oa=None)
    assert res["overwrite"]["journal"] == "Journal of the American Chemical & Society"


# ------------------------------------------------------------------ run
def test_富化把短刊名换成全称(tmp_path, monkeypatch):
    """回归:SQL 是 COALESCE(NULLIF(...)) 只填空,邮件带来的 "Org. Lett."
    永远换不成全称,于是按刊名查 easyScholar 期刊等级一直查不到。"""
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "t.db")
    cfg.journal_rank.enabled = False

    conn = db.Database(cfg.db_file).connect()
    iid, _ = db.upsert_item(conn, {
        "kind": "paper", "dedup_key": "doi:10.1/x", "doi": "10.1/x",
        "title": "T", "title_norm": "t", "source": "xmol",
        "journal": "Org. Lett.", "abstract": "邮件里就有的摘要",
        "authors": json.dumps(["旧作者"]),
    })
    conn.commit()
    conn.close()

    monkeypatch.setattr(enrich.semanticscholar, "fetch_many_by_doi",
                        lambda *a, **kw: {"10.1/x": {"journal": "Org. Lett."}})
    monkeypatch.setattr(enrich.crossref_search, "fetch_many_by_doi",
                        lambda *a, **kw: {"10.1/x": {
                            "journal": "Organic Letters", "issn": "1523-7060",
                            "authors": ["Real A", "Real B"],
                            "abstract": "Crossref 的摘要"}})

    enrich.run(cfg, verbose=False)

    conn = db.Database(cfg.db_file).connect()
    row = conn.execute("SELECT * FROM item WHERE id = ?", (iid,)).fetchone()
    assert row["journal"] == "Organic Letters", "Crossref 刊名必须覆盖缩写"
    assert row["issn"] == "1523-7060"
    assert json.loads(row["authors"]) == ["Real A", "Real B"]
    assert row["abstract"] == "邮件里就有的摘要", "摘要只填空,不该被覆盖"
    conn.close()
