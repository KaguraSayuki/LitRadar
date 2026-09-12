from __future__ import annotations

import email
from email.message import EmailMessage
from email import policy
from html import escape
from urllib.parse import quote

import pytest

from litradar.sources import wos_email


ALERT_ID = "12345678-1234-1234-1234-1234567890ab"


def _mail(
    subject: str,
    body: str,
    *,
    sender: str = "alerts-noreply@clarivate.com",
    message_id: str = "<wos-test@example.invalid>",
) -> bytes:
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["Date"] = "Sat, 12 Sep 2026 10:00:00 +0000"
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content("plain fallback")
    msg.add_alternative(body, subtype="html")
    return bytes(msg)


def _wrapped_url(target: str) -> str:
    snowplow = "https://redirect.example.invalid/click?u=" + quote(target, safe="")
    return "https://nam12.safelinks.protection.outlook.com/?url=" + quote(
        snowplow, safe=""
    )


def test_parse_multilayer_safelink_and_metadata() -> None:
    target = f"https://www.webofscience.com/wos/alldb/alert-execution-summary/{ALERT_ID}"
    wrapped = _wrapped_url(target)
    raw = _mail(
        "Web of Science Alert - Optical sensors - 2 results",
        f'<p><a href="{escape(wrapped, quote=True)}">View all <b>2</b> records</a></p>',
    )

    alert, meta = wos_email.parse_bytes(raw)

    assert alert == wos_email.WosAlert(
        alert_id=ALERT_ID,
        url=target,
        total=2,
        query="Optical sensors",
    )
    assert meta == {
        "message_id": "<wos-test@example.invalid>",
        "subject": "Web of Science Alert - Optical sensors - 2 results",
        "received_at": "Sat, 12 Sep 2026 10:00:00 +0000",
        "from": "alerts-noreply@clarivate.com",
    }


def test_public_canonical_url_has_same_offline_policy() -> None:
    target = f"https://www.webofscience.com/wos/alldb/alert-execution-summary/{ALERT_ID}"

    assert wos_email.canonical_wos_url(_wrapped_url(target)) == target


def test_forward_prefix_is_supported_when_original_sender_is_retained() -> None:
    target = f"https://www.webofscience.com/wos/alldb/alert-execution-summary/{ALERT_ID}"
    raw = _mail(
        "Fw: Web of Science Alert - catalyst - 1 result",
        f'<a href="{target}">View all 1 record</a>',
        message_id="<outer-retained@example.invalid>",
    )

    alert, meta = wos_email.parse_bytes(raw)

    assert alert is not None and alert.alert_id == ALERT_ID
    assert meta["message_id"] == "<outer-retained@example.invalid>"


def test_forward_with_changed_sender_requires_original_from_header() -> None:
    target = f"https://www.webofscience.com/wos/alldb/alert-execution-summary/{ALERT_ID}"
    body = f"""<pre>---------- Forwarded message ---------
From: alerts-noreply@clarivate.com
Subject: Web of Science Alert - catalyst - 1 result
</pre><a href="{target}">View all 1 record</a>"""
    raw = _mail(
        "Fwd: Web of Science Alert - catalyst - 1 result",
        body,
        sender="researcher@example.invalid",
        message_id="<outer-inline@example.invalid>",
    )

    alert, meta = wos_email.parse_bytes(raw)

    assert alert is not None and alert.total == 1
    assert meta["message_id"] == "<outer-inline@example.invalid>"


def test_changed_sender_subject_alone_is_not_enough() -> None:
    target = f"https://www.webofscience.com/wos/alldb/alert-execution-summary/{ALERT_ID}"
    raw = _mail(
        "Fwd: Web of Science Alert - catalyst - 1 result",
        f'<a href="{target}">View all 1 record</a>',
        sender="researcher@example.invalid",
    )

    alert, _ = wos_email.parse_bytes(raw)

    assert alert is None


def test_forwarded_rfc822_attachment_keeps_outer_message_id() -> None:
    target = f"https://www.webofscience.com/wos/alldb/alert-execution-summary/{ALERT_ID}"
    inner = _mail(
        "Web of Science Alert - catalyst - 1 result",
        f'<a href="{target}">View all 1 record</a>',
    )
    outer = EmailMessage()
    outer["Message-ID"] = "<outer-rfc822@example.invalid>"
    outer["From"] = "researcher@example.invalid"
    outer["Subject"] = "Fwd: Web of Science Alert - catalyst - 1 result"
    outer.set_content("Forwarded as attachment")
    outer.add_attachment(
        email.message_from_bytes(inner, policy=policy.default), subtype="rfc822"
    )

    alert, meta = wos_email.parse_bytes(bytes(outer))

    assert alert is not None and alert.alert_id == ALERT_ID
    assert meta["message_id"] == "<outer-rfc822@example.invalid>"


def test_parse_clarivate_cn_and_zero_results() -> None:
    target = f"https://webofscience.clarivate.cn/wos/woscc/alert-execution-summary/{ALERT_ID}"
    raw = _mail(
        "Web of Science Alert - catalyst - 0 results",
        f'<a href="{target}">View all 0 records</a>',
    )

    alert, _ = wos_email.parse_bytes(raw)

    assert alert is not None
    assert alert.total == 0
    assert alert.url == target


def test_foreign_final_host_is_rejected() -> None:
    target = f"https://evil.example.invalid/wos/alldb/alert-execution-summary/{ALERT_ID}"
    raw = _mail(
        "Web of Science Alert - catalyst - 1 result",
        f'<a href="{_wrapped_url(target)}">View all 1 record</a>',
    )

    with pytest.raises(ValueError, match="Web of Science"):
        wos_email.parse_bytes(raw)


def test_alert_without_matching_link_is_an_error() -> None:
    raw = _mail(
        "Web of Science Alert - catalyst - 1 result",
        "<p>Your alert is ready, but the records link is unavailable.</p>",
    )

    with pytest.raises(ValueError, match="缺少"):
        wos_email.parse_bytes(raw)


def test_non_alert_mail_is_ignored() -> None:
    raw = _mail(
        "Web of Science account notice",
        '<a href="https://evil.example.invalid">View all 1 record</a>',
    )

    alert, meta = wos_email.parse_bytes(raw)

    assert alert is None
    assert meta["subject"] == "Web of Science account notice"


def test_alert_subject_from_other_sender_is_ordinary_mail() -> None:
    raw = _mail(
        "Web of Science Alert - catalyst - 1 result",
        "<p>not a real alert</p>",
        sender="someone@example.invalid",
    )

    alert, _ = wos_email.parse_bytes(raw)

    assert alert is None


def test_malformed_alert_subject_is_rejected() -> None:
    raw = _mail("Web of Science Alert - catalyst", "<p>missing count</p>")

    with pytest.raises(ValueError, match="主题"):
        wos_email.parse_bytes(raw)


def test_control_characters_in_the_subject_never_reach_the_stored_query() -> None:
    """query 会被 CLI/日志原样打印,主题里的控制字符不该能注入终端。"""
    target = f"https://www.webofscience.com/wos/alldb/alert-execution-summary/{ALERT_ID}"
    raw = _mail(
        "Web of Science Alert - che\x1b[31nmistry\x07 - 2 results",
        f'<p><a href="{escape(target, quote=True)}">View all 2 records</a></p>',
    )

    alert, _ = wos_email.parse_bytes(raw)

    assert alert is not None
    assert not any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in alert.query)
    assert "mistry" in alert.query
