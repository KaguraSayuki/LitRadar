"""Offline rendering checks for the Web of Science queue on the stats page."""
from __future__ import annotations

from litradar.web.app import templates


def _stats(**extra):
    value = {
        "items": 0,
        "new": 0,
        "starred": 0,
        "ignored": 0,
        "scored": 0,
        "summaries": 0,
        "by_source": [],
        "by_journal": [],
        "feedback": [],
        "last_runs": [],
    }
    value.update(extra)
    return value


def _render(stats):
    return templates.get_template("stats.html").render(
        request=None,
        today="2026-09-12",
        llm_ready=False,
        static_v="test",
        home_label="首页",
        page="stats",
        s=stats,
    )


def test_stats_shows_wos_source_queue_status_and_counts():
    html = _render(_stats(
        by_source=[{"source": "wos", "n": 3}],
        wos_counts={"pending": 1, "retry": 2, "needs_login": 1, "complete": 4},
        wos_alerts=[
            {
                "query": "chemistry",
                "expected_count": 12,
                "records_imported": 7,
                "status": "retry",
                "attempts": 3,
                "next_attempt_at": "2026-09-12T12:00:00+00:00",
                "last_error": "temporary failure",
                "updated_at": "2026-09-12T11:00:00+00:00",
            },
            {
                "query": "materials",
                "expected_count": 4,
                "records_imported": 0,
                "status": "needs_login",
                "attempts": 1,
                "next_attempt_at": None,
                "last_error": "login required",
                "updated_at": "2026-09-12T10:00:00+00:00",
            },
            {
                "query": "completed query",
                "expected_count": 2,
                "records_imported": 2,
                "status": "complete",
                "attempts": 1,
                "next_attempt_at": None,
                "last_error": None,
                "updated_at": "2026-09-12T09:00:00+00:00",
            },
        ],
    ))

    assert "Web of Science" in html
    assert "等待采集" in html
    assert "等待重试" in html
    assert "需要重新登录" in html
    assert "已完成" in html
    assert "实际 7 / 12" in html
    assert "实际 2 / 2" in html
    assert "<b>2</b>等待重试" in html


def test_stats_escapes_queue_text_and_hides_url_or_token_from_errors():
    html = _render(_stats(
        wos_alerts=[
            {
                "query": "<img src=x onerror=alert(1)>",
                "expected_count": 1,
                "records_imported": 0,
                "status": "retry",
                "attempts": 1,
                "next_attempt_at": None,
                "last_error": "<script>alert('xss')</script>",
                "updated_at": "",
            },
            {
                "query": "safe query",
                "expected_count": 1,
                "records_imported": 0,
                "status": "retry",
                "attempts": 1,
                "next_attempt_at": None,
                "last_error": "GET https://example.invalid/?access_token=SECRET",
                "updated_at": "",
            },
        ],
    ))

    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;" in html
    assert "<script>alert" not in html
    assert "https://example.invalid" not in html
    assert "SECRET" not in html
    assert "失败详情已隐藏" in html


def test_stats_remains_compatible_when_old_stats_has_no_wos_keys():
    html = _render(_stats())
    assert "Web of Science 采集" in html
    assert "还没有 Web of Science 采集任务。" in html
