"""Offline parser for Web of Science RIS exports."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..normalize import normalize_doi


# 字段标签只认列 0 的大写双字符。按 RIS 规范,续行一律缩进;若允许行首
# 空白,摘要里 "Co-doped" / "In-situ" / "Py-based" / "Do-doped" 这类
# 句子会被当成 CO/IN/PY/DO 字段:该行从摘要中消失,且后续真正的续行
# 也挂到伪标签下,整段摘要被截成第一行,PY 甚至会被伪值顶掉而丢掉年份。
_TAG_RE = re.compile(r"^(?P<tag>[A-Z0-9]{2})[ \t]*-[ \t]?(?P<value>.*)$")
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_HTML_ERROR_RE = re.compile(r"^\s*(?:<!doctype\s+html|<html\b|<head\b|<body\b)", re.I)
_MONTHS = {
    "JAN": 1, "JANUARY": 1,
    "FEB": 2, "FEBRUARY": 2,
    "MAR": 3, "MARCH": 3,
    "APR": 4, "APRIL": 4,
    "MAY": 5,
    "JUN": 6, "JUNE": 6,
    "JUL": 7, "JULY": 7,
    "AUG": 8, "AUGUST": 8,
    "SEP": 9, "SEPT": 9, "SEPTEMBER": 9,
    "OCT": 10, "OCTOBER": 10,
    "NOV": 11, "NOVEMBER": 11,
    "DEC": 12, "DECEMBER": 12,
}


def _decode(raw: bytes) -> str:
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("raw must be bytes")
    try:
        return bytes(raw).decode("utf-8-sig")
    except UnicodeDecodeError:
        # RIS exports from older desktop clients are sometimes Windows-1252.
        # The structural validation below still rejects arbitrary/binary data.
        try:
            return bytes(raw).decode("cp1252")
        except UnicodeDecodeError as exc:
            raise ValueError("RIS 文件编码无效") from exc


def _append_field(record: dict[str, list[str]], tag: str, value: str) -> None:
    record.setdefault(tag, []).append(value.strip())


def _parse_records(text: str) -> list[dict[str, list[str]]]:
    if not text.strip():
        raise ValueError("RIS 文件为空")
    if _HTML_ERROR_RE.match(text):
        raise ValueError("RIS 下载结果是 HTML 错误页")

    records: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    last_tag: str | None = None
    after_er = False

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip("\ufeff")
        match = _TAG_RE.match(line)
        if match:
            tag = match.group("tag").upper()
            value = match.group("value").strip()
            if tag == "TY":
                if current is not None:
                    raise ValueError(f"RIS 第 {lineno} 行缺少 ER")
                if after_er:
                    after_er = False
                current = {"TY": [value]}
                last_tag = "TY"
                continue

            if current is None:
                raise ValueError(f"RIS 第 {lineno} 行在 TY 之前")
            if tag == "ER":
                records.append(current)
                current = None
                last_tag = None
                after_er = True
                continue
            _append_field(current, tag, value)
            last_tag = tag
            after_er = False
            continue

        if not line.strip():
            continue
        if current is None:
            if after_er:
                raise ValueError(f"RIS 第 {lineno} 行在记录之间无法解析")
            raise ValueError(f"RIS 第 {lineno} 行不是 RIS 字段")
        if last_tag is None:
            raise ValueError(f"RIS 第 {lineno} 行续行没有前置字段")
        # RIS continuation lines belong to the preceding field.  Joining with
        # a space retains word boundaries in abstracts/titles while avoiding
        # accidental line-break punctuation in exported text.
        continuation = line.strip()
        if current[last_tag]:
            current[last_tag][-1] = f"{current[last_tag][-1]} {continuation}".strip()
        else:
            current[last_tag].append(continuation)

    if current is not None:
        raise ValueError("RIS 文件截断，末尾缺少 ER")
    if not records:
        raise ValueError("RIS 文件没有记录")
    return records


def _first(record: dict[str, list[str]], *tags: str) -> str | None:
    for tag in tags:
        for value in record.get(tag, []):
            value = value.strip()
            if value:
                return value
    return None


def _all(record: dict[str, list[str]], tag: str) -> list[str]:
    return [value.strip() for value in record.get(tag, []) if value.strip()]


def _doi(record: dict[str, list[str]]) -> str | None:
    value = _first(record, "DO")
    if not value:
        return None
    # WoS has emitted both ``10.x/...`` and ``DOI: 10.x/...`` forms, as well
    # as doi.org URLs, in the same export family.
    value = re.sub(r"^\s*doi\s*:\s*", "", value, flags=re.IGNORECASE)
    # DO is a structured RIS field.  Do not strip punctuation heuristically:
    # DOI suffixes may legally end in punctuation, and changing it here can
    # merge two distinct structured identifiers.  Text extraction has its own
    # sentence-boundary cleanup in normalize.find_doi().
    return normalize_doi(value)


def _year(value: str | None) -> int | None:
    if not value:
        return None
    match = _YEAR_RE.search(value)
    return int(match.group(1)) if match else None


def _valid_date(year: int | None, month: int = 1, day: int = 1) -> str | None:
    if year is None:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _date_value(value: str | None, default_year: int | None) -> str | None:
    """Parse WoS DA/C6 values while validating with ``datetime.date``.

    WoS exports use values such as ``2013 OCT 1``, ``2024 AUG`` and ``FEB``;
    the latter relies on PY for its year.  Invalid days are reduced to the
    validated first day of that month, never emitted as an impossible date.
    """
    if not value:
        return None
    text = value.strip().upper()
    explicit_year = _year(text)
    year = explicit_year or default_year

    # Numeric ISO-like forms, including a year/month value with no day.
    full = re.search(
        r"(?<!\d)((?:19|20)\d{2})\s*[-/.]\s*(\d{1,2})"
        r"(?:\s*[-/.]\s*(\d{1,2}))?(?!\d)", text,
    )
    if full:
        year = int(full.group(1))
        month = int(full.group(2))
        day = int(full.group(3) or 1)
        return _valid_date(year, month, day) or _valid_date(year, month, 1)

    tokens = re.findall(r"[A-Z]+|\d+", text)
    month_index: int | None = None
    month: int | None = None
    for index, token in enumerate(tokens):
        if token in _MONTHS:
            month_index, month = index, _MONTHS[token]
            break
    if month is None:
        # A bare PY/DA year is a valid year-only publication date.
        return _valid_date(year)

    day: int | None = None
    # A numeric token adjacent to the month is the day unless it is a year.
    for index in (month_index + 1, month_index - 1):
        if index is None or not 0 <= index < len(tokens):
            continue
        token = tokens[index]
        if token.isdigit() and not _YEAR_RE.fullmatch(token):
            day = int(token)
            break
    if day is None:
        day = 1
    return _valid_date(year, month, day) or _valid_date(year, month, 1)


def _published_at(record: dict[str, list[str]]) -> str | None:
    # DA is the publication date in WoS RIS.  Y2 is the export date and must
    # never become an item's publication date.  PY supplies the year when DA
    # contains only a month (for example ``DA - FEB``).
    py_year = _year(_first(record, "PY"))
    da = _first(record, "DA")
    parsed = _date_value(da, py_year)
    if parsed:
        return parsed

    # Some exports have only an Early Access month in C6.  Use it only when
    # DA is absent and its explicit year agrees with PY; never use Y2.
    if not da:
        c6 = _first(record, "C6")
        c6_year = _year(c6)
        if c6 and (py_year is None or c6_year in (None, py_year)):
            parsed = _date_value(c6, py_year)
            if parsed:
                return parsed
    return _valid_date(py_year)


def _record_url(doi: str | None, accession: str) -> str:
    if doi:
        return "https://doi.org/" + quote(doi, safe="/")
    # Search alerts can span All Databases, so do not restrict the link to
    # Core Collection. Keep the accession colon readable in the record URL.
    return "https://www.webofscience.com/wos/alldb/full-record/" + quote(
        accession, safe=":"
    )


def _record_to_item(record: dict[str, list[str]]) -> dict[str, Any]:
    accession = _first(record, "AN")
    if not accession:
        raise ValueError("RIS 记录缺少 WoS accession AN")
    title = _first(record, "TI", "T1")
    if not title:
        raise ValueError(f"RIS 记录 {accession} 缺少标题 TI")

    doi = _doi(record)
    ris_type = (_first(record, "TY") or "").strip().upper()
    kind = "patent" if ris_type in {"PAT", "PATENT"} else "paper"
    return {
        "kind": kind,
        # 无 DOI 时留空,交给 pipeline._prepare 退化成 title: 键 —— 与
        # Crossref / OpenAlex / X-MOL 一致。用 wos:<AN> 永远撞不上同一篇
        # 文献的 title: 键,同一篇会被入库两次。
        "dedup_key": f"doi:{doi}" if doi else None,
        "doi": doi,
        "title": title,
        "title_norm": None,
        "abstract": _first(record, "AB"),
        "authors": _all(record, "AU"),
        "journal": _first(record, "T2", "JO"),
        "issn": _first(record, "SN"),
        "published_at": _published_at(record),
        "url": _first(record, "UR", "L1", "L2") or _record_url(doi, accession),
        "impact_factor": None,
        "xmol_url": None,
        "matched_keywords": [],
        "source": "wos",
        "source_ref": accession,
    }


def parse_bytes(raw: bytes) -> list[dict[str, Any]]:
    """Parse a WoS RIS export into dictionaries accepted by ``pipeline._store``."""
    records = _parse_records(_decode(raw))
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for record in records:
        accession = _first(record, "AN")
        if not accession:
            raise ValueError("RIS 记录缺少 WoS accession AN")
        accession_key = accession.casefold()
        if accession_key in seen:
            raise ValueError(f"RIS 包含重复 WoS accession: {accession}")
        seen.add(accession_key)
        out.append(_record_to_item(record))
    return out


def parse_file(path: str | Path) -> list[dict[str, Any]]:
    return parse_bytes(Path(path).read_bytes())


__all__ = ["parse_bytes", "parse_file"]
