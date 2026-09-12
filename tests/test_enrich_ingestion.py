"""采集携带部分元数据时,仍需完成权威富化且不能抹掉已取得的数据。"""
from unittest.mock import Mock

import pytest

from litradar import db, enrich, pipeline
from litradar.config import Config
from litradar.sources import crossref_search, semanticscholar


def test_s2_metadata_without_doi_survives_ingestion(tmp_path):
    conn = db.Database(tmp_path / "t.db").connect()
    data = semanticscholar._to_dict({
        "title": "Paper without DOI", "externalIds": {},
        "authors": [{"name": "Alice Chemist"}], "citationCount": 5,
        "url": "https://www.semanticscholar.org/paper/123",
        "openAccessPdf": {"url": "https://example.invalid/paper.pdf"},
    })
    iid, _ = pipeline._store(conn, data)
    row = conn.execute(
        "SELECT i.authors, i.url, e.cited_by_count, e.is_oa, e.oa_url FROM item i "
        "JOIN item_enrichment e ON e.item_id=i.id WHERE i.id=?", (iid,),
    ).fetchone()
    assert tuple(row) == ('["Alice Chemist"]',
                          "https://www.semanticscholar.org/paper/123", 5, 1,
                          "https://example.invalid/paper.pdf")
    assert enrich._pending(conn, 100) == []
    conn.close()


@pytest.mark.parametrize("reverse", [False, True])
def test_doi_case_merges_sources_in_both_orders(tmp_path, reverse):
    records = [
        semanticscholar._to_dict({"title": "Same paper",
                                  "externalIds": {"DOI": "10.9999/ABC"}}),
        crossref_search._msg_to_item({"title": ["Same paper"], "DOI": "10.9999/abc"}),
    ]
    conn = db.Database(tmp_path / "t.db").connect()
    ids = [pipeline._store(conn, record)[0]
           for record in (reversed(records) if reverse else records)]
    assert ids[0] == ids[1]
    assert [tuple(r) for r in conn.execute("SELECT doi, dedup_key FROM item")] == [
        ("10.9999/abc", "doi:10.9999/abc")]
    conn.close()


def test_s2_metadata_does_not_skip_full_enrichment_or_erase_it(tmp_path, monkeypatch):
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "t.db")
    cfg.journal_rank.enabled = False
    doi = "10.9999/metadata"
    response = {"title": "Metadata paper", "externalIds": {"DOI": doi.upper()},
                "abstract": "An abstract already supplied by search.", "venue": "Org. Lett.",
                "authors": [{"name": "Search Author"}], "citationCount": 5,
                "openAccessPdf": {"url": "https://example.invalid/paper.pdf"}}
    conn = db.Database(cfg.db_file).connect()
    iid, _ = pipeline._store(conn, semanticscholar._to_dict(response))
    conn.commit()
    stored = conn.execute("SELECT cited_by_count, enriched_at FROM item_enrichment "
                          "WHERE item_id=?", (iid,)).fetchone()
    assert tuple(stored) == (5, None)

    cr = Mock(return_value={doi: {"journal": "Organic Letters", "issn": "1523-7060",
                                 "authors": ["Canonical Author"]}})
    s2 = Mock(return_value={doi: {"cited_by_count": 6,
                                 "oa_url": "https://example.invalid/paper.pdf"}})
    monkeypatch.setattr(enrich.crossref_search, "fetch_many_by_doi", cr)
    monkeypatch.setattr(enrich.semanticscholar, "fetch_many_by_doi", s2)
    assert enrich.run(cfg, verbose=False)["checked"] == 1
    row = conn.execute("SELECT journal, issn, authors FROM item WHERE id=?", (iid,)).fetchone()
    assert tuple(row) == ("Organic Letters", "1523-7060", '["Canonical Author"]')
    full = conn.execute("SELECT crossref_json, enriched_at FROM item_enrichment "
                        "WHERE item_id=?", (iid,)).fetchone()
    assert full[0] and full[1]

    # 下次检索只更新来源已给出的引用数,不能清空权威元数据/完成时间。
    response["citationCount"] = 7
    pipeline._store(conn, semanticscholar._to_dict(response))
    conn.commit()
    after = conn.execute("SELECT cited_by_count, crossref_json, enriched_at "
                         "FROM item_enrichment WHERE item_id=?", (iid,)).fetchone()
    assert tuple(after) == (7, full[0], full[1])
    assert enrich.run(cfg, verbose=False)["checked"] == 0
    assert cr.call_count == s2.call_count == 1
    conn.close()
