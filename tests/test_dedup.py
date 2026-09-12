"""去重、归一化与期刊匹配测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from litradar.normalize import (  # noqa: E402
    find_doi,
    normalize_doi,
    parse_date,
    split_authors,
    title_norm,
)
from litradar.rank import journal_initials, journal_matches  # noqa: E402


# ------------------------------------------------------------------ DOI
@pytest.mark.parametrize("raw,expected", [
    ("10.1021/acs.orglett.6c03259", "10.1021/acs.orglett.6c03259"),
    ("doi:10.1021/ABC.123", "10.1021/abc.123"),
    ("https://doi.org/10.1002/anie.7232106", "10.1002/anie.7232106"),
    ("DOI:10.1021/ABC.", "10.1021/abc."),
    ("10.1234/ABC;", "10.1234/abc;"),
    ("10.1234/ABC)", "10.1234/abc)"),
    ("10.1234/ABC(D)", "10.1234/abc(d)"),
])
def test_doi_归一化(raw, expected):
    assert normalize_doi(raw) == expected


def test_从文本里抓doi():
    text = "Org. Lett. (IF 4.7) 2026-09-04 DOI:10.1021/acs.orglett.6c03259 作者"
    assert find_doi(text) == "10.1021/acs.orglett.6c03259"
    assert find_doi("没有 DOI 的文本") is None
    assert find_doi("DOI:10.1021/acs.joc.6c01762.") == "10.1021/acs.joc.6c01762"
    assert find_doi("(DOI:10.1234/ABC(D)).") == "10.1234/abc(d)"
    assert find_doi("(DOI:10.1234/ABC);") == "10.1234/abc"


# ------------------------------------------------------------------ 标题
def test_标题归一化():
    a = title_norm("Photochemical Reaction of Vinyldiazo Esters and Azides!")
    b = title_norm("photochemical reaction of vinyldiazo esters and azides")
    assert a == b




# ------------------------------------------------------------------ 期刊
@pytest.mark.parametrize("full,abbr", [
    ("Organic Letters", "Org. Lett."),
    ("Journal of the American Chemical Society", "J. Am. Chem. Soc."),
    ("Angewandte Chemie International Edition", "Angew. Chem. Int. Ed."),
    ("The Journal of Organic Chemistry", "J. Org. Chem."),
    ("Chemical Science", "Chem. Sci."),
])
def test_期刊缩写与全称首字母一致(full, abbr):
    assert journal_initials(full) == journal_initials(abbr)


def test_期刊白名单匹配():
    core = ["Organic Letters", "Journal of the American Chemical Society"]
    # X-MOL 邮件给的是缩写,必须能匹配上白名单里的全称
    assert journal_matches("Org. Lett.", core) == "Organic Letters"
    assert journal_matches("J. Am. Chem. Soc.", core) == "Journal of the American Chemical Society"
    assert journal_matches("Journal of the American Chemical Society", core) is not None
    assert journal_matches("Journal of Irreproducible Results", core) is None


# ------------------------------------------------------------------ 其他
def test_作者拆分():
    assert split_authors("Xin Li,Jian-Ting Sun,Yu Zhang") == [
        "Xin Li", "Jian-Ting Sun", "Yu Zhang"]
    assert split_authors("A. B. Smith; C. D. Jones") == ["A. B. Smith", "C. D. Jones"]


def test_日期解析():
    assert parse_date("2026-09-04") == "2026-09-04"
    assert parse_date("2026/9/4") == "2026-09-04"
    assert parse_date("") is None
