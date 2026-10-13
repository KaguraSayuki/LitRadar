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


def test_venue_queries_run_without_general_s2_queries(tmp_path, monkeypatch):
    cfg = config(tmp_path, s2_venue_queries=["synthetic query"])
    cfg.sources.s2_search_enabled = True
    cfg.sources.s2_venues = ["Synthetic Journal"]
    monkeypatch.setattr(pipeline, "read_secret", lambda *_: "synthetic-key")
    search = Mock(return_value=[paper("10.9999/venue")])
    monkeypatch.setattr(semanticscholar, "search_bulk", search)
    result = pipeline.ingest_keyword_search(cfg, verbose=False)
    assert result["new"] == 1 and result["errors"] == 0
    assert search.call_args.args == ("synthetic query",)
    assert search.call_args.kwargs["venues"] == ["Synthetic Journal"]


def test_removed_seed_cannot_contribute_to_current_threshold(tmp_path, monkeypatch):
    cfg = config(tmp_path, seed_dois=["10.9999/seed-b", "10.9999/seed-c"])
    cfg.sources.snowball_enabled = True
    cfg.sources.snowball_min_cocitations = 2
    conn = db.Database(cfg.db_file).connect()
    profile = load_groups(cfg)[0]
    gid = db.sync_groups(conn, [profile])[profile.slug]
    candidate = paper("10.9999/candidate")
    db.save_seed_cites(conn, "10.9999/removed-seed-a", [candidate], group_id=gid)
    conn.commit()
    assert db.cocited_items(conn, 2, group_id=gid) == []
    monkeypatch.setattr(pipeline, "read_secret", lambda *_: "synthetic-key")
    monkeypatch.setattr(semanticscholar, "fetch_citations", lambda ref, **k:
                        [candidate] if ref.endswith("seed-b") else [])
    stats = {}
    assert pipeline._ingest_snowball(conn, cfg, profile, gid, stats, verbose=False) == []
    assert stats["snowball_failed"] == 0  # A successful empty response is not failure.
    assert conn.execute("SELECT COUNT(*) FROM seed_cite").fetchone()[0] == 2
    db.save_seed_cites(conn, "10.9999/seed-c", [candidate], group_id=gid)
    assert db.cocited_items(conn, 2, group_id=gid, seed_dois=profile.seed_dois)[0]["source_ref"] == "cocite:2"
    conn.close()


def test_source_outage_keeps_other_queries_and_partial_s2_pages(tmp_path, monkeypatch):
    # Query/group names may be "errors" without being confused with a counter.
    cfg = config(tmp_path, slug="errors", search_queries=["errors", "unavailable"],
                 s2_queries=["synthetic"])
    cfg.sources.crossref_search_enabled = cfg.sources.s2_search_enabled = True
    cfg.sources.s2_search_max_pages = 2
    monkeypatch.setattr(pipeline, "read_secret", lambda *_: "synthetic-key")
    monkeypatch.setattr(semanticscholar, "read_secret", lambda *_: "synthetic-key")
    monkeypatch.setattr(http.time, "sleep", lambda *_: None)
    monkeypatch.setattr(http, "_throttle", lambda *_: None)
    monkeypatch.setattr(semanticscholar, "_throttle", lambda *_: None)
    def crossref_get(url, *, params, **kwargs):
        if params["query.bibliographic"] == "unavailable":
            return Mock(status_code=503)
        return Mock(status_code=200, json=lambda: {"message": {"items": [
            {"title": ["Successful synthetic query"], "DOI": "10.9999/crossref"}]}})
    def s2_get(url, *, params, **kwargs):
        if params.get("token"):
            return Mock(status_code=503)
        return Mock(status_code=200, json=lambda: {"data": [
            {"title": "Successful synthetic page", "externalIds": {"DOI": "10.9999/s2"}}],
            "token": "second-page"})
    monkeypatch.setattr(http.SESSION, "get", crossref_get)
    monkeypatch.setattr(semanticscholar.requests, "get", s2_get)
    result = pipeline.ingest_keyword_search(cfg, verbose=False)
    assert result["new"] == 2 and result["errors"] == 2
    assert result["groups"]["errors"]["per_query"]["errors"] == 1
    assert progress.has_errors(result) and cli._result_exit_code(result) == 1
    conn = db.Database(cfg.db_file).connect()
    assert {row[0] for row in conn.execute("SELECT doi FROM item")} == {
        "10.9999/crossref", "10.9999/s2"}
    assert conn.execute("SELECT status FROM run_log").fetchone()[0] == "partial"
    conn.close()


@pytest.mark.parametrize("source", ["crossref", "openalex"])
def test_successful_empty_source_response_is_not_failure(monkeypatch, source):
    adapter = getattr(pipeline, f"{source}_search")
    payload = {"message": {"items": []}} if source == "crossref" else {"results": []}
    monkeypatch.setattr(adapter, "get_json", lambda *a, **kw: payload)
    assert adapter.search("synthetic query") == []


@pytest.mark.parametrize("operation", ["search", "citations"])
def test_s2_malformed_later_record_preserves_earlier_records(monkeypatch, operation):
    monkeypatch.setattr(semanticscholar, "read_secret", lambda *_: "synthetic-key")
    monkeypatch.setattr(semanticscholar, "_throttle", lambda *_: None)
    valid = {"title": "Synthetic valid record", "externalIds": {"DOI": "10.9999/valid"},
             "publicationDate": "2026-10-01"}
    records = [valid, None] if operation == "search" else [{"citingPaper": valid}, None]
    monkeypatch.setattr(semanticscholar.requests, "get", lambda *a, **kw:
                        Mock(status_code=200, json=lambda: {"data": records}))
    with pytest.raises(http.SourceFetchError) as caught:
        if operation == "search":
            semanticscholar.search_bulk("synthetic query")
        else:
            semanticscholar.fetch_citations("DOI:10.9999/seed")
    assert [p["doi"] for p in caught.value.partial] == ["10.9999/valid"]


@pytest.mark.parametrize("source", ["crossref", "s2"])
def test_strict_batch_failure_keeps_successful_record(monkeypatch, source):
    if source == "crossref":
        adapter = pipeline.crossref_search
        monkeypatch.setattr(adapter, "get_json", lambda *a, **kw: {"message": {"items": [
            {"title": ["Synthetic valid paper"], "DOI": "10.9999/valid"}, None]}})
        operation = lambda: adapter.fetch_many_by_doi(["10.9999/valid", "10.9999/other"], strict=True)
    else:
        monkeypatch.setattr(semanticscholar, "_throttle", lambda *_: None)
        monkeypatch.setattr(semanticscholar.time, "sleep", lambda *_: None)
        monkeypatch.setattr(semanticscholar.requests, "post", lambda *a, **kw:
                            Mock(status_code=200, json=lambda: [
                                {"title": "Synthetic valid paper"}, "malformed record"]))
        operation = lambda: semanticscholar.fetch_many_by_doi(
            ["10.9999/valid", "10.9999/other"], strict=True, retries=1)
    with pytest.raises(http.SourceFetchError) as caught:
        operation()
    assert set(caught.value.partial) == {"10.9999/valid"}


def test_strict_s2_batch_accepts_null_for_unindexed_doi(monkeypatch):
    monkeypatch.setattr(semanticscholar, "_throttle", lambda *_: None)
    monkeypatch.setattr(semanticscholar.requests, "post", lambda *a, **kw:
                        Mock(status_code=200, json=lambda: [None]))
    assert semanticscholar.fetch_many_by_doi(["10.9999/unindexed"], strict=True) == {}
