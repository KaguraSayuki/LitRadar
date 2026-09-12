"""外链清洗:入库口只放行 http(s),模板 href 才不会变成可点的 XSS。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from litradar import db  # noqa: E402


@pytest.mark.parametrize("raw, want", [
    ("https://doi.org/10.1/a", "https://doi.org/10.1/a"),
    ("HTTP://example.org/x", "HTTP://example.org/x"),
    ("  https://x.org/p?q=1 ", "https://x.org/p?q=1"),
    ("javascript:alert(1)", None),
    ("JavaScript:alert(1)", None),
    ("data:text/html;base64,AAAA", None),
    ("ftp://example.org/f", None),
    ("//example.org/x", None),
    ("", None),
    (None, None),
])
def test_只放行http(raw, want):
    assert db._clean_url(raw) == want


def test_入库时清洗item外链(tmp_path):
    database = db.Database(tmp_path / "t.db")
    conn = database.connect()
    iid, _ = db.upsert_item(conn, {
        "kind": "paper", "dedup_key": "doi:10.1/a", "doi": "10.1/a",
        "title": "T", "title_norm": "t", "source": "xmol",
        "url": "javascript:alert(1)", "xmol_url": "https://www.x-mol.com/paper/1",
    })
    row = conn.execute("SELECT url, xmol_url FROM item WHERE id=?", (iid,)).fetchone()
    assert row["url"] is None
    assert row["xmol_url"] == "https://www.x-mol.com/paper/1"

    # 补全路径同样经过清洗:坏链接不能借"只填空"钻进来
    db.upsert_item(conn, {
        "kind": "paper", "dedup_key": "doi:10.1/a", "doi": "10.1/a",
        "title": "T", "title_norm": "t", "source": "crossref",
        "url": "data:text/html,<script>",
    })
    assert conn.execute("SELECT url FROM item WHERE id=?", (iid,)).fetchone()[0] is None
    conn.close()


def test_富化时清洗oa链接(tmp_path):
    database = db.Database(tmp_path / "t.db")
    conn = database.connect()
    iid, _ = db.upsert_item(conn, {
        "kind": "paper", "dedup_key": "doi:10.1/a", "doi": "10.1/a",
        "title": "T", "title_norm": "t", "source": "xmol",
    })
    db.save_enrichment(conn, iid, {"is_oa": 1, "oa_url": "javascript:alert(1)"})
    assert conn.execute("SELECT oa_url FROM item_enrichment WHERE item_id=?",
                        (iid,)).fetchone()[0] is None
    db.save_enrichment(conn, iid, {"is_oa": 1, "oa_url": "https://oa.example/p.pdf"})
    assert conn.execute("SELECT oa_url FROM item_enrichment WHERE item_id=?",
                        (iid,)).fetchone()[0] == "https://oa.example/p.pdf"
    conn.close()
