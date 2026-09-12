"""Parse Web of Science search-alert messages.

The alert mail is deliberately treated as an input document, rather than as
an invitation to follow arbitrary links.  A message is recognised only when
the sender and the subject have the shape used by Clarivate search alerts.
The record link is then unwrapped locally through the ``url``/``u`` query
parameters used by Safe Links and Clarivate redirects.  No network request is
made here.
"""
from __future__ import annotations

import email
import html as htmlmod
import re
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID


_WOS_SENDER = "alerts-noreply@clarivate.com"
_SUBJECT_RE = re.compile(
    r"^\s*web\s+of\s+science\s+alert\s*-\s*"
    r"(?P<query>.*?)\s*-\s*(?P<total>[\d,]+)\s+results?\s*$",
    re.IGNORECASE,
)
_SUBJECT_HINT_RE = re.compile(r"\bweb\s+of\s+science\s+alert\b", re.IGNORECASE)
_ANCHOR_RE = re.compile(
    r"\bview\s+all\s+(?P<count>[\d,]+)\s+records?\b", re.IGNORECASE
)
_WOS_PATH_RE = re.compile(
    r"^/wos/(?P<database>[A-Za-z0-9_-]+)/alert-execution-summary/"
    r"(?P<alert_id>[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})/?$",
)
_FINAL_HOSTS = {"www.webofscience.com", "webofscience.clarivate.cn"}
_LINK_QUERY_KEYS = {"url", "u"}
PARSE_VERSION = 1
_FORWARD_PREFIX_RE = re.compile(
    r"^(?:(?:fw|fwd)\s*:\s*|转发\s*[:：]\s*)+", re.IGNORECASE
)
_FORWARDED_FROM_RE = re.compile(
    r"^[ \t]*(?:from|发件人)[ \t]*:[ \t]*(?P<value>[^\r\n]+)",
    re.IGNORECASE | re.MULTILINE,
)
_FORWARDED_SUBJECT_RE = re.compile(
    r"^[ \t]*(?:subject|主题)[ \t]*:[ \t]*(?P<value>[^\r\n]+)",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class WosAlert:
    """The actionable part of one Web of Science search alert."""

    alert_id: str
    url: str
    total: int
    query: str


class _AnchorParser(HTMLParser):
    """Collect anchor text and hrefs without depending on a browser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        # HTMLParser has already normalised attribute names.  Keep the first
        # href only; a malformed duplicate attribute must not change the link
        # selected by the parser later.
        self._href = next((v for k, v in attrs if k.lower() == "href" and v), None)
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        self.links.append((" ".join(self._text), self._href))
        self._href = None
        self._text = []

    def close(self) -> None:
        # A broken message with an unterminated anchor is still not a valid
        # alert link.  Do not silently accept text accumulated after it.
        super().close()
        self._href = None
        self._text = []


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        value = part.get_payload()
        return value if isinstance(value, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def _body_parts(msg) -> list[tuple[str, str]]:
    """Return HTML/text body parts, excluding attachments."""
    if msg.is_multipart():
        parts = [
            (part.get_content_type(), _decode_part(part))
            for part in msg.walk()
            if part.get_content_disposition() != "attachment"
            and part.get_content_type() in {"text/html", "text/plain"}
        ]
        # Prefer HTML when both alternatives exist, but retain plain text as a
        # fallback for messages generated without an HTML part.
        return sorted(parts, key=lambda item: item[0] != "text/html")
    content_type = msg.get_content_type()
    return [(content_type, _decode_part(msg))] if content_type in {
        "text/html", "text/plain"
    } else []


def _meta(msg) -> dict:
    # Keep these names and value conventions aligned with xmol_email.parse_bytes
    # so callers can persist either kind of mail through the same boundary.
    return {
        "message_id": (msg.get("Message-ID") or "").strip() or None,
        "subject": str(msg.get("Subject") or ""),
        "received_at": str(msg.get("Date") or ""),
        "from": str(msg.get("From") or ""),
    }


def _subject_without_forward_prefix(subject: str) -> tuple[str, bool]:
    value = subject.strip()
    forwarded = False
    while True:
        match = _FORWARD_PREFIX_RE.match(value)
        if not match:
            return value, forwarded
        value = value[match.end():].strip()
        forwarded = True


def _plain_forward_text(body: str, content_type: str) -> str:
    if content_type != "text/html":
        return body
    text = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    text = re.sub(r"</(?:div|p|li|tr|blockquote)>", "\n", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return htmlmod.unescape(text)


def _forwarded_headers(parts: list[tuple[str, str]]) -> Iterator[tuple[str, str]]:
    """Yield original From/Subject pairs from an inline forwarded body.

    The caller must already know that the outer subject is a forward.  This
    header pair is the trust boundary for a normal forward whose outer From
    address belongs to the user, so a quoted subject by itself is never enough.
    """
    for content_type, body in parts:
        text = _plain_forward_text(body, content_type)
        for from_match in _FORWARDED_FROM_RE.finditer(text):
            window = text[from_match.end():from_match.end() + 4000]
            subject_match = _FORWARDED_SUBJECT_RE.search(window)
            if subject_match:
                yield from_match.group("value").strip(), subject_match.group("value").strip()


def _nested_messages(msg: Message) -> Iterator[Message]:
    """Yield attached ``message/rfc822`` messages without opening URLs."""
    for part in msg.walk():
        if part is msg or part.get_content_type() != "message/rfc822":
            continue
        payload = part.get_payload()
        candidates = payload if isinstance(payload, list) else [payload]
        yielded = False
        for candidate in candidates:
            if isinstance(candidate, Message):
                yield candidate
                yielded = True
                continue
            if isinstance(candidate, (bytes, bytearray)):
                yield email.message_from_bytes(bytes(candidate), policy=policy.default)
                yielded = True
        if candidates and not yielded:
            encoded = part.get_payload(decode=True)
            if isinstance(encoded, (bytes, bytearray)) and encoded:
                yield email.message_from_bytes(bytes(encoded), policy=policy.default)


def _normalise_anchor_text(text: str) -> str:
    return " ".join(htmlmod.unescape(text).replace("\xa0", " ").split())


def _iter_links(parts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    for content_type, body in parts:
        if content_type == "text/html":
            parser = _AnchorParser()
            try:
                parser.feed(body)
                parser.close()
            except Exception:  # malformed HTML is handled as a missing link
                parser.links = []
            links.extend(parser.links)
            continue

        # Plain text alerts occasionally contain a rendered ``View all ...``
        # line followed by its URL rather than an HTML anchor.
        plain = htmlmod.unescape(body)
        for match in re.finditer(
            r"(?P<label>view\s+all\s+[\d,]+\s+records?)\b.{0,600}?"
            r"(?P<url>https?://[^\s<>\"']+)",
            plain,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            links.append((match.group("label"), match.group("url")))
    return links


def _decode_url_text(value: str) -> str:
    value = htmlmod.unescape(value or "").strip()
    # Quotes are common when the nested value was copied out of an HTML
    # attribute or a JSON redirect parameter.
    value = value.strip(" \t\r\n\"'")
    for _ in range(6):
        # Keep an already-formed URL intact.  Decoding its complete query
        # string here would turn an encoded ``%26`` inside a nested target
        # into an outer separator before parse_qs gets a chance to isolate the
        # target value.  A value that is itself encoded has no URL scheme yet,
        # so it is safe to decode until it becomes one.
        parsed = urlsplit(value)
        if parsed.scheme and parsed.netloc:
            break
        decoded = unquote(value).strip(" \t\r\n\"'")
        if decoded == value:
            break
        value = decoded
    return value


def _canonical_wos_url(url: str, *, depth: int = 0, seen: frozenset[str] = frozenset()) -> str:
    """Recursively unwrap a Safe Links/Clarivate URL and validate its target."""
    if depth > 6:
        raise ValueError("Web of Science 链接重定向层级过深")
    current = _decode_url_text(url)
    if not current or current in seen:
        raise ValueError("Web of Science 链接重定向循环或为空")
    seen = seen | {current}

    try:
        parsed = urlsplit(current)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Web of Science 链接无效") from exc

    # Every layer must be an HTTPS URL.  This prevents mail text from turning
    # a parser call into an accidental javascript/data/http navigation.
    if parsed.scheme.lower() != "https" or not host:
        raise ValueError("Web of Science 链接必须使用 HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Web of Science 链接包含不允许的凭据")

    if host in _FINAL_HOSTS:
        if port not in (None, 443):
            raise ValueError("Web of Science 链接端口不受支持")
        path = unquote(parsed.path)
        match = _WOS_PATH_RE.fullmatch(path)
        if not match:
            raise ValueError("Web of Science 链接路径不是 alert-execution-summary")
        try:
            alert_id = str(UUID(match.group("alert_id")))
        except ValueError as exc:  # regex shape is not sufficient for UUID validity
            raise ValueError("Web of Science alert ID 无效") from exc
        database = match.group("database")
        return f"https://{host}/wos/{database}/alert-execution-summary/{alert_id}"

    query = parse_qs(parsed.query, keep_blank_values=True)
    nested: list[str] = []
    for key, values in query.items():
        if key.lower() in _LINK_QUERY_KEYS:
            nested.extend(values)
    if not nested:
        raise ValueError("Web of Science 链接缺少 url/u 重定向参数")

    last_error: ValueError | None = None
    for candidate in nested:
        try:
            return _canonical_wos_url(candidate, depth=depth + 1, seen=seen)
        except ValueError as exc:
            last_error = exc
    raise last_error or ValueError("Web of Science 链接目标无效")


def canonical_wos_url(url: str) -> str:
    """Return a validated WoS alert URL after offline redirect unwrapping.

    Browser integrations can call this public boundary before navigation; it
    performs no network access and applies the same HTTPS/host/path policy as
    the mail parser.
    """
    return _canonical_wos_url(url)


def _select_link(parts: list[tuple[str, str]], total: int) -> str:
    # Require a validated execution-summary link even for zero results: without
    # it there is no stable alert identity to queue. Unrecognised templates stay
    # unacknowledged so a later parser update can retry them.
    candidates: list[str] = []
    for label, href in _iter_links(parts):
        match = _ANCHOR_RE.search(_normalise_anchor_text(label))
        if not match or int(match.group("count").replace(",", "")) != total:
            continue
        candidates.append(htmlmod.unescape(href))
    if not candidates:
        raise ValueError("Web of Science alert 缺少 View all records 链接")

    last_error: ValueError | None = None
    for candidate in candidates:
        try:
            return _canonical_wos_url(candidate)
        except ValueError as exc:
            last_error = exc
    raise last_error or ValueError("Web of Science alert 链接无效")


def _alert_from_subject(subject: str, parts: list[tuple[str, str]]) -> WosAlert | None:
    core_subject, _ = _subject_without_forward_prefix(subject)
    subject_match = _SUBJECT_RE.fullmatch(core_subject)
    if not subject_match:
        if _SUBJECT_HINT_RE.search(core_subject):
            raise ValueError("Web of Science alert 主题格式无效")
        return None
    query = subject_match.group("query").strip()
    if not query:
        raise ValueError("Web of Science alert 查询为空")
    total = int(subject_match.group("total").replace(",", ""))
    url = _select_link(parts, total)
    path_match = _WOS_PATH_RE.fullmatch(urlsplit(url).path)
    if path_match is None:  # canonical_wos_url already validates this
        raise ValueError("Web of Science alert 链接路径无效")
    return WosAlert(
        alert_id=str(UUID(path_match.group("alert_id"))),
        url=url,
        total=total,
        query=query,
    )


def _alert_from_message(msg: Message, *, depth: int = 0) -> WosAlert | None:
    """Find an alert in one message while keeping forwarding trust explicit."""
    if depth > 4:
        raise ValueError("Web of Science 转发层级过深")
    meta = _meta(msg)
    address = parseaddr(meta["from"])[1].strip().lower()
    subject = meta["subject"]
    core_subject, is_forward = _subject_without_forward_prefix(subject)
    parts = _body_parts(msg)

    if address == _WOS_SENDER:
        # The original sender may remain in the outer headers after an Fw/Fwd
        # operation.  Its body is trusted exactly like a direct alert.
        alert = _alert_from_subject(core_subject, parts)
        if alert is not None:
            return alert
        if not is_forward:
            return None
    elif not is_forward:
        # A changed outer sender cannot become a WoS alert merely by copying a
        # WoS-looking subject into an ordinary message.
        # A message/rfc822 attachment below is independently authenticated by
        # its own original From/Subject and may still be inspected.
        pass
    else:
        # Inline forwarding keeps the original headers in the body.  Require
        # both From and Subject from that block before considering its link.
        for original_from, original_subject in _forwarded_headers(parts):
            original_address = parseaddr(original_from)[1].strip().lower()
            if original_address != _WOS_SENDER:
                continue
            alert = _alert_from_subject(original_subject, parts)
            if alert is not None:
                return alert

    # Outlook and other clients may attach the original as message/rfc822
    # instead of quoting it inline.  Recurse into that message and return only
    # the alert; parse_bytes() deliberately retains the outer metadata.
    for nested in _nested_messages(msg):
        alert = _alert_from_message(nested, depth=depth + 1)
        if alert is not None:
            return alert
    return None


def parse_bytes(raw: bytes) -> tuple[WosAlert | None, dict]:
    """Parse one message, returning ``(alert, meta)``.

    Ordinary mail is intentionally ignored.  Once both the exact Clarivate
    alert sender and the alert subject shape identify a search alert, malformed
    content is an error so that ingestion can retry rather than silently ack a
    bad message.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("raw must be bytes")
    msg = email.message_from_bytes(bytes(raw), policy=policy.default)
    meta = _meta(msg)
    return _alert_from_message(msg), meta


def parse_file(path: str | Path) -> tuple[WosAlert | None, dict]:
    return parse_bytes(Path(path).read_bytes())


__all__ = ["WosAlert", "canonical_wos_url", "parse_bytes", "parse_file"]
