"""X-MOL 订阅邮件解析器。

邮件结构(实测样本 fixtures/xmol_sample.eml):

    亲爱的用户，您选择的文献：
    <span style="font-weight: bold">1. 标题(<span style="color:#ff0000">命中词</span>…)</span><br>
    <em>Org. Lett.</em>(IF <span>4.7</span>)2026-09-04 DOI:10.1021/... <a originalsrc="…">链接</a><br>
    作者1,作者2,作者3<br>
    2. …

要点:
  * 邮件只给**题录**,没有摘要 -> 摘要靠 DOI 走 OpenAlex 富化。
  * 链接被 Outlook Safelinks 包了一层 -> 优先取 ``originalsrc``。
  * 高亮词 ``color:#ff0000`` 是命中的订阅关键词,属于有价值的元数据。
  * 邮件是精选 teaser(通常 2 条),不是全量结果集。
"""
from __future__ import annotations

import email
import html as htmlmod
import json
import re
from dataclasses import asdict, dataclass, field
from email import policy
from pathlib import Path

from ..normalize import clean_html_text, find_doi, parse_date, split_authors

PARSE_VERSION = 1

# 条目起点:<span style="font-weight: bold">1.
_ENTRY_RE = re.compile(r'font-weight:\s*bold"?>\s*(\d+)\.')
# 页脚标志:从这里往后属于模板,不是条目内容
_FOOTER_RE = re.compile(r"还有更多|subscribe_thesis|X-MOL客服|祝您生活愉快")
_RED_RE = re.compile(r'color:\s*#ff0000[^>]*>(.*?)</span>', re.I | re.S)


@dataclass
class XmolRecord:
    num: int
    title: str
    journal: str | None = None
    impact_factor: float | None = None
    published_at: str | None = None
    doi: str | None = None
    xmol_url: str | None = None
    authors: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def unwrap_outlook(url: str) -> str:
    """拆掉 Outlook Safelinks 包装。"""
    if "safelinks.protection.outlook.com" not in url:
        return url
    import urllib.parse

    p = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return p.get("url", [url])[0]


def decode_part(part) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def html_body(msg) -> str:
    """取邮件 HTML 正文;没有 HTML 时退化为纯文本再包一层。"""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return decode_part(part)
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return "<pre>" + decode_part(part) + "</pre>"
        return ""
    return decode_part(msg)


def parse_bytes(raw: bytes) -> tuple[list[XmolRecord], dict]:
    """解析一封 .eml 的字节流,返回 (条目列表, 邮件头信息)。"""
    msg = email.message_from_bytes(raw, policy=policy.default)
    meta = {
        "message_id": (msg.get("Message-ID") or "").strip() or None,
        "subject": str(msg.get("Subject") or ""),
        "received_at": str(msg.get("Date") or ""),
        "from": str(msg.get("From") or ""),
    }
    return parse_html(html_body(msg)), meta


def parse_file(path: str | Path) -> tuple[list[XmolRecord], dict]:
    return parse_bytes(Path(path).read_bytes())


def parse_html(h: str) -> list[XmolRecord]:
    starts = [(m.start(), int(m.group(1))) for m in _ENTRY_RE.finditer(h)]
    out: list[XmolRecord] = []

    for i, (pos, num) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(h)
        seg = h[pos:end]

        foot = _FOOTER_RE.search(seg)
        if foot:
            seg = seg[: foot.start()]

        m = re.search(r'bold"?>\s*\d+\.(.*?)</span>\s*<br', seg, re.S)
        title = clean_html_text(m.group(1), merge_commas=True) if m else ""
        title = _strip_leading_number(title, num)
        if not title:
            continue

        keywords = [clean_html_text(k) for k in _RED_RE.findall(seg)]
        keywords = [k for k in keywords if k]

        rec = XmolRecord(
            num=num,
            title=title,
            doi=find_doi(seg),
            matched_keywords=keywords,
        )

        mj = re.search(r"<em>(.*?)</em>", seg, re.S)
        if mj:
            rec.journal = clean_html_text(mj.group(1)) or None

        mif = re.search(r"IF\s*(?:&nbsp;)?\s*</span>\s*<span[^>]*>\s*([\d.]+)", seg)
        if mif:
            try:
                rec.impact_factor = float(mif.group(1))
            except ValueError:
                pass

        md = re.search(r"\)\s*(\d{4}-\d{2}-\d{2})", seg)
        if md:
            rec.published_at = parse_date(md.group(1))

        for lm in re.finditer(r'originalsrc="([^"]+)"', seg):
            u = unwrap_outlook(htmlmod.unescape(lm.group(1)))
            if "x-mol.com/paper/" in u:
                rec.xmol_url = u
                break

        ma = re.search(r"</a>(?:&nbsp;|\s)*<br>(.*?)<br", seg, re.S)
        if ma:
            rec.authors = split_authors(ma.group(1))

        out.append(rec)

    return out


def _strip_leading_number(title: str, num: int) -> str:
    t = title.strip()
    m = re.match(rf"^{num}\s*[.、]\s*", t)
    return t[m.end():].strip() if m else t


def to_item_data(rec: XmolRecord, source_ref: str | None = None) -> dict:
    """转成 db.upsert_item 需要的字段。"""
    doi = rec.doi
    return {
        "kind": "paper",
        "dedup_key": f"doi:{doi}" if doi else f"title:{_fallback_key(rec.title)}",
        "doi": doi,
        "title": rec.title,
        "title_norm": None,             # 由 pipeline 填
        "abstract": None,               # 邮件没有摘要,靠富化
        "authors": rec.authors,
        "journal": rec.journal,
        "issn": None,
        "published_at": rec.published_at,
        "url": rec.xmol_url,
        "impact_factor": rec.impact_factor,
        "xmol_url": rec.xmol_url,
        "matched_keywords": rec.matched_keywords,
        "source": "xmol",
        "source_ref": source_ref,
    }


def _fallback_key(title: str) -> str:
    from ..normalize import title_norm

    return title_norm(title)[:120]


def dumps(recs: list[XmolRecord]) -> str:
    return json.dumps([r.as_dict() for r in recs], ensure_ascii=False, indent=2)
