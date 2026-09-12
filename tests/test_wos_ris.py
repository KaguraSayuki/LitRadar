from __future__ import annotations

import pytest

from litradar.sources import wos_ris


VALID_RIS = """TY  - JOUR
AU  - Doe, Jane
AU  - Smith, John
TI  - A title with
  a continuation line
T2  - Journal of Tests
AB  - First abstract line
  and its continuation.
SN  - 1234-5678
DA  - 2003/04/05
PY  - 2003
DO  - DOI: 10.5555/ABC.
AN  - WOS:0000001
Y2  - 2026/09/12
ER  -

TY  - JOUR
AU  - Roe, Alex
TI  - A record without a DOI
T2  - Another Journal
PY  - 2024
AN  - WOS:0000002
Y2  - 2026/09/12
ER  -
"""


def test_parse_records_into_pipeline_item_dicts() -> None:
    records = wos_ris.parse_bytes(VALID_RIS.encode())

    assert len(records) == 2
    first, second = records
    assert first["source"] == "wos"
    assert first["source_ref"] == "WOS:0000001"
    assert first["doi"] == "10.5555/abc."
    assert first["dedup_key"] == "doi:10.5555/abc."
    assert first["title"] == "A title with a continuation line"
    assert first["abstract"] == "First abstract line and its continuation."
    assert first["authors"] == ["Doe, Jane", "Smith, John"]
    assert first["published_at"] == "2003-04-05"
    assert first["url"] == "https://doi.org/10.5555/abc."
    assert second["doi"] is None
    assert second["dedup_key"] is None
    assert second["abstract"] is None
    assert second["published_at"] == "2024-01-01"
    assert second["url"] == (
        "https://www.webofscience.com/wos/alldb/full-record/WOS:0000002"
    )


def test_y2_export_date_is_not_publication_date() -> None:
    ris = b"""TY  - JOUR
TI  - Only an export date
AN  - WOS:0000003
Y2  - 2099/12/31
ER  -
"""

    record = wos_ris.parse_bytes(ris)[0]

    assert record["published_at"] is None


def test_month_only_da_uses_py_and_validates_calendar_dates() -> None:
    ris = b"""TY  - JOUR
TI  - Month only
DA  - FEB
PY  - 2024
AN  - WOS:0000004
ER  -

TY  - JOUR
TI  - Invalid day is reduced safely
DA  - 2024 FEB 30
PY  - 2024
AN  - WOS:0000005
ER  -
"""

    records = wos_ris.parse_bytes(ris)

    assert records[0]["published_at"] == "2024-02-01"
    assert records[1]["published_at"] == "2024-02-01"


def test_pat_ris_record_maps_to_patent_kind() -> None:
    ris = b"""TY  - PAT
TI  - A patent record
AN  - WOS:PAT-0001
PY  - 2024
ER  -
"""

    record = wos_ris.parse_bytes(ris)[0]

    assert record["kind"] == "patent"


def test_duplicate_accession_is_rejected() -> None:
    ris = b"""TY  - JOUR
TI  - One
AN  - WOS:DUPLICATE
ER  -
TY  - JOUR
TI  - Two
AN  - wos:duplicate
ER  -
"""

    with pytest.raises(ValueError, match="重复"):
        wos_ris.parse_bytes(ris)


def test_truncated_record_is_rejected() -> None:
    ris = b"TY  - JOUR\nTI  - No end marker\nAN  - WOS:TRUNCATED\n"

    with pytest.raises(ValueError, match="ER"):
        wos_ris.parse_bytes(ris)


def test_missing_title_is_rejected() -> None:
    ris = b"TY  - JOUR\nAN  - WOS:NO-TITLE\nER  -\n"

    with pytest.raises(ValueError, match="标题"):
        wos_ris.parse_bytes(ris)


def test_html_error_page_is_rejected() -> None:
    with pytest.raises(ValueError, match="HTML"):
        wos_ris.parse_bytes(b"<!doctype html><html><body>download failed</body></html>")


# ------------------------------------------------ 缩进续行不能被当成字段标签
# 摘要里以"元素/词根 + 连字符"开头的句子极常见(Co-doped、In-situ、Py-based)。
# 旧正则允许行首空白,会把它们读成 CO/IN/PY/DO/ER 字段:那一行从摘要里消失,
# 后续真正的续行也挂到伪标签下,整段摘要被截成第一行。
def test_indented_continuation_lines_stay_in_the_abstract() -> None:
    ris = (
        "TY  - JOUR\n"
        "AN  - WOS:0000009\n"
        "TI  - Oxygen evolution on doped oxides\n"
        "AB  - We studied a series of transition-metal oxides.\n"
        "  Co-doped and Fe-doped samples were prepared.\n"
        "  In-situ XRD confirmed the phase purity.\n"
        "  Py-based ligands were used as precursors.\n"
        "PY  - 2024\n"
        "ER  -\n"
    ).encode()

    record = wos_ris.parse_bytes(ris)[0]

    assert record["abstract"] == (
        "We studied a series of transition-metal oxides. "
        "Co-doped and Fe-doped samples were prepared. "
        "In-situ XRD confirmed the phase purity. "
        "Py-based ligands were used as precursors."
    )
    # PY 曾被 "Py-based…" 伪值顶掉,年份整段丢失(文献会掉出排序时间窗)。
    assert record["published_at"] == "2024-01-01"
    assert record["doi"] is None


def test_do_prefixed_continuation_does_not_fabricate_a_doi() -> None:
    ris = (
        "TY  - JOUR\n"
        "AN  - WOS:0000010\n"
        "TI  - A title\n"
        "AB  - First line.\n"
        "  Do-doped samples were prepared by impregnation.\n"
        "PY  - 2024\n"
        "ER  -\n"
    ).encode()

    record = wos_ris.parse_bytes(ris)[0]

    assert record["doi"] is None
    assert record["dedup_key"] is None
    assert record["abstract"] == (
        "First line. Do-doped samples were prepared by impregnation.")


def test_er_prefixed_continuation_does_not_end_the_record() -> None:
    ris = (
        "TY  - JOUR\n"
        "AN  - WOS:0000011\n"
        "TI  - A title\n"
        "AB  - First line.\n"
        "  Er-doped fibre amplifiers were used.\n"
        "PY  - 2024\n"
        "ER  -\n"
    ).encode()

    record = wos_ris.parse_bytes(ris)[0]

    assert record["source_ref"] == "WOS:0000011"
    assert record["published_at"] == "2024-01-01"
    assert record["abstract"] == (
        "First line. Er-doped fibre amplifiers were used.")


def test_doi_less_records_get_a_title_key_from_the_pipeline() -> None:
    """缺 DOI 的记录必须退化成 title: 键,否则同一篇文献会被入库两次。"""
    from litradar.pipeline import _prepare

    ris = b"TY  - JOUR\nTI  - A record without a DOI\nAN  - WOS:0000002\nPY  - 2024\nER  -\n"

    prepared = _prepare(wos_ris.parse_bytes(ris)[0])

    assert prepared["dedup_key"] == "title:a record without a doi"
