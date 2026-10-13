"""Synthetic coverage for source merging, query isolation and legacy keys."""
from unittest.mock import Mock

import pytest

from litradar import cli, db, http, pipeline, progress
from litradar.config import Config
from litradar.normalize import title_norm
from litradar.rank import load_groups
from litradar.sources import semanticscholar, xmol_email


def config(tmp_path, **group):
    cfg = Config()
    cfg.app.db_path = str(tmp_path / "papers.db")
    cfg.interests_data = {"groups": [{"slug": "synthetic", **group}]}
    cfg.sources.crossref_search_enabled = False
    cfg.sources.s2_search_enabled = False
    cfg.sources.openalex_enabled = False
    cfg.sources.snowball_enabled = False
    return cfg


def paper(doi, **values):
    return {"kind": "paper", "title": "Synthetic paper", "doi": doi,
            "source": "crossref", **values}


@pytest.mark.parametrize("source", ["s2", "xmol"])
def test_full_title_keys_preserve_distinct_papers_and_legacy_state(tmp_path, source):
    def convert(title):
        if source == "s2":
            return semanticscholar._to_dict({"title": title, "externalIds": {}})
        return xmol_email.to_item_data(xmol_email.XmolRecord(num=1, title=title))

    prefix = "Synthetic investigation of " + "materials and methods " * 7
    first_title, second_title = prefix + "first experiment", prefix + "second experiment"
    conn = db.Database(tmp_path / "keys.db").connect()
    first, _ = pipeline._store(conn, convert(first_title))
    # Reconstruct the key used by pre-fix installations, with user state attached.
    conn.execute("UPDATE item SET dedup_key=? WHERE id=?",
                 (f"title:{title_norm(first_title)[:120]}", first))
    db.set_action(conn, first, "star")
    second, status = pipeline._store(conn, convert(second_title))
    assert status == "created" and second != first
    assert pipeline._store(conn, convert(first_title)) == (first, "duplicate")
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 2
    assert conn.execute("SELECT dedup_key FROM item WHERE id=?", (first,)).fetchone()[0] == \
        f"title:{title_norm(first_title)}"
    assert first in db.starred_ids(conn)
    assert conn.execute("SELECT item_id FROM feedback").fetchone()[0] == first
    conn.close()


def test_same_run_merges_complementary_source_metadata(tmp_path, monkeypatch):
    cfg = config(tmp_path, search_queries=["synthetic"], s2_queries=["synthetic"])
    cfg.sources.crossref_search_enabled = cfg.sources.s2_search_enabled = True
    monkeypatch.setattr(pipeline, "read_secret", lambda *_: "synthetic-key")
    monkeypatch.setattr(pipeline.crossref_search, "search", lambda *a, **k: [
        paper("10.9999/MERGED", journal="Canonical Synthetic Journal", published_at="2026-01-01")])
    monkeypatch.setattr(semanticscholar, "search_bulk", lambda *a, **k: [
        semanticscholar._to_dict({"title": "Synthetic paper", "externalIds": {"DOI": "10.9999/merged"},
                                  "venue": "Short J.", "abstract": "Available synthetic abstract",
                                  "citationCount": 0, "authors": [{"name": "Synthetic Author"}],
                                  "publicationDate": "2026-09-17",
                                  "openAccessPdf": {"url": "https://example.invalid/paper.pdf"}})])
    result = pipeline.ingest_keyword_search(cfg, verbose=False)
    assert result["new"] == result["unique"] == 1 and result["errors"] == 0
    conn = db.Database(cfg.db_file).connect()
    row = conn.execute("SELECT i.journal,i.abstract,i.authors,e.cited_by_count,e.oa_url,i.published_at "
                       "FROM item i JOIN item_enrichment e ON e.item_id=i.id").fetchone()
    assert tuple(row) == ("Canonical Synthetic Journal", "Available synthetic abstract",
                          '["Synthetic Author"]', 0, "https://example.invalid/paper.pdf", "2026-09-17")
    conn.close()
