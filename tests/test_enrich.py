"""富化测试:Crossref 的规范字段必须真的覆盖旧值;缺摘要的条目不能无限重试(不联网)。"""
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


# ------------------------------------------------------- 缺摘要重试上限
def _seed(cfg, doi: str, *, abstract: str | None = None) -> int:
    conn = db.Database(cfg.db_file).connect()
    iid, _ = db.upsert_item(conn, {
        "kind": "paper", "dedup_key": f"doi:{doi}", "doi": doi,
        "title": doi, "title_norm": doi, "source": "xmol", "abstract": abstract,
    })
    conn.commit()
    conn.close()
    return iid


def _attempts(cfg, iid: int) -> int:
    conn = db.Database(cfg.db_file).connect()
    try:
        return conn.execute("SELECT abstract_attempts FROM item_enrichment WHERE item_id=?",
                            (iid,)).fetchone()[0]
    finally:
        conn.close()


def test_没拿到摘要的条目每轮计一次(tmp_path, monkeypatch):
    """回归:来源真没有摘要的条目以前每轮都被 _pending 重新拉进来陪跑批量请求。"""
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "t.db")
    cfg.journal_rank.enabled = False
    dry = _seed(cfg, "10.1/dry")                          # 两个源都不给摘要
    wet = _seed(cfg, "10.1/wet")                          # S2 给了摘要
    monkeypatch.setattr(enrich.semanticscholar, "fetch_many_by_doi",
                        lambda *a, **kw: {"10.1/wet": {"abstract": "有了"}})
    monkeypatch.setattr(enrich.crossref_search, "fetch_many_by_doi",
                        lambda *a, **kw: {"10.1/dry": {"journal": "J"},
                                          "10.1/wet": {"journal": "J"}})

    for n in range(1, enrich.MAX_ABSTRACT_ATTEMPTS + 2):
        enrich.run(cfg, verbose=False)
        assert _attempts(cfg, dry) == min(n, enrich.MAX_ABSTRACT_ATTEMPTS)
        assert _attempts(cfg, wet) == 0, "拿到摘要的条目不该计数"

    conn = db.Database(cfg.db_file).connect()
    pending = {int(r["id"]) for r in enrich._pending(conn, 100)}
    conn.close()
    assert dry not in pending, "试够次数之后必须退出重试队列"
    assert wet not in pending


def test_没试够次数的仍会重试(tmp_path):
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "t.db")
    iid = _seed(cfg, "10.1/a")
    conn = db.Database(cfg.db_file).connect()
    db.save_enrichment(conn, iid, {})
    conn.execute("UPDATE item_enrichment SET abstract_attempts=? WHERE item_id=?",
                 (enrich.MAX_ABSTRACT_ATTEMPTS - 1, iid))
    conn.commit()
    assert [int(r["id"]) for r in enrich._pending(conn, 100)] == [iid]
    conn.close()


def test_重新富化不会把计数归零(tmp_path):
    """save_enrichment 的 UPSERT 只动自己列出的列,计数必须留着。"""
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "t.db")
    iid = _seed(cfg, "10.1/a")
    conn = db.Database(cfg.db_file).connect()
    db.save_enrichment(conn, iid, {"cited_by_count": 1})
    conn.execute("UPDATE item_enrichment SET abstract_attempts=3 WHERE item_id=?", (iid,))
    db.save_enrichment(conn, iid, {"cited_by_count": 2})
    conn.commit()
    row = conn.execute("SELECT abstract_attempts, cited_by_count FROM item_enrichment "
                       "WHERE item_id=?", (iid,)).fetchone()
    assert tuple(row) == (3, 2)
    conn.close()
