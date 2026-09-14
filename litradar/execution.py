"""Run-frequency policy shared by web, CLI, and the scheduler."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import db

STAGES = {
    "mail": ("ingest_mail",), "search": ("ingest_search",), "enrich": ("enrich",),
    "rank": ("rank",), "summarize": ("summarize",),
    "all": ("ingest_mail", "ingest_search", "enrich", "rank", "summarize"),
}


class ExecutionLimit(RuntimeError):
    def __init__(self, message, retry=None):
        super().__init__(message)
        self.retry = retry


def guarded(cfg, stage: str) -> bool:
    return stage in cfg.admin.guarded_stages or (stage == 'all' and bool(cfg.admin.guarded_stages))


def check_limits(cfg, stage, group_slug=None, *, now=None):
    """Called while holding the pipeline lock, immediately before execution."""
    if not guarded(cfg, stage):
        return
    limits = cfg.admin
    if not limits.cooldown_seconds and not limits.daily_limit:
        return
    protected = {n for s in limits.guarded_stages for n in STAGES[s]}
    names = set(STAGES.get(stage, (stage,))) & protected
    conn = db.Database(cfg.db_file).connect()
    try:
        # An unbounded daily count is needed: a recent-row cap lets busy
        # installations age earlier runs out of their daily allowance.
        rows = db.recent_stage_runs(conn, names, group_slug=group_slug, limit=-1)
    finally:
        conn.close()
    local_zone = ZoneInfo(cfg.app.timezone)
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(local_zone).date()
    times, counts = [], {}
    for name, raw in rows:
        try:
            when = datetime.fromisoformat(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        times.append(when)
        if when.astimezone(local_zone).date() == today:
            counts[name] = counts.get(name, 0) + 1
    if times and limits.cooldown_seconds:
        waited = (now - max(times)).total_seconds()
        if waited < limits.cooldown_seconds:
            retry = int(limits.cooldown_seconds - waited) + 1
            raise ExecutionLimit(f"这个阶段刚运行过，还要等 {retry} 秒后才能重试。", retry)
    if counts and limits.daily_limit and max(counts.values()) >= limits.daily_limit:
        raise ExecutionLimit(f"这个方向的相关阶段今天已达到 {limits.daily_limit} 次上限。请明天再试，或在 AI 与阅读中调整每日次数。")


def check_groups(cfg, stage, groups):
    if not groups or stage in ('mail', 'enrich'):
        check_limits(cfg, stage)
    else:
        for group in groups:
            check_limits(cfg, stage, group.slug)
