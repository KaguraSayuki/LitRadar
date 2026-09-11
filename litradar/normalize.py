"""归一化:DOI、标题、作者、日期。去重与匹配的基础。"""
from __future__ import annotations

import html as htmlmod
import re
import unicodedata
from datetime import date, datetime

_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def find_doi(text: str | None) -> str | None:
    """从任意文本里抓第一个 DOI。"""
    if not text:
        return None
    m = _DOI_RE.search(text)
    if not m:
        return None
    return normalize_doi(m.group(0))


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip().lower()
    doi = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    doi = doi.rstrip(".,;)")
    return doi or None


def title_norm(title: str | None) -> str:
    """标题归一化:去 HTML/标点/空白/大小写,用于模糊去重。"""
    if not title:
        return ""
    t = htmlmod.unescape(_TAG_RE.sub(" ", title))
    t = unicodedata.normalize("NFKD", t)
    t = t.lower()
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", t)
    return _WS_RE.sub(" ", t).strip()




def clean_html_text(seg: str, *, merge_commas: bool = False) -> str:
    """HTML 片段转纯文本。专门处理 X-MOL 邮件里的硬断行与 span 切断。

    - ``Example-\\n  A``  -> ``Example-A``
    - ``α\\n  -Sample`` -> ``α-Sample``
    - ``Example-A , B``   -> ``Example-A,B``(merge_commas=True)
    """
    seg = re.sub(r"<br\s*/?>", "\n", seg, flags=re.I)
    seg = _TAG_RE.sub("", seg)
    seg = htmlmod.unescape(seg)
    seg = seg.replace("\xa0", " ").replace("\u200b", "")
    seg = re.sub(r"[ \t]+", " ", seg)
    seg = re.sub(r"-\s*\n\s*", "-", seg)
    seg = re.sub(r"\s+-\s*", "-", seg)
    if merge_commas:
        seg = re.sub(r"\s*,\s*", ",", seg)
    seg = re.sub(r"\n\s*", " ", seg)
    return _WS_RE.sub(" ", seg).strip()


def split_authors(raw: str | None) -> list[str]:
    """把 ``A,B,C`` / ``A; B; C`` / ``A and B`` 拆成作者列表。"""
    if not raw:
        return []
    raw = clean_html_text(raw)
    parts = re.split(r"\s*(?:,|;|\band\b|·)\s*", raw)
    return [p.strip() for p in parts if p.strip() and len(p.strip()) > 1]


def parse_date(value: str | None) -> str | None:
    """尽力解析成 ISO8601 日期字符串(仅日期部分)。"""
    if not value:
        return None
    value = value.strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", value)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    for fmt in ("%d %B %Y", "%B %d, %Y", "%Y-%m", "%Y"):
        try:
            d = datetime.strptime(value, fmt)
            return d.date().isoformat()
        except ValueError:
            continue
    return None


def days_ago(n: int) -> str:
    from datetime import timedelta

    return (date.today() - timedelta(days=n)).isoformat()
