"""A single, persistent scheduler process; never registered in web workers."""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import credentials, db, enrich, execution, pipeline, rank, summarize
from .config import load_config
from .lock import AlreadyRunning, single_instance
from .settings import SettingsError


def validate(cfg):
    schedule = cfg.schedule
    try:
        ZoneInfo(cfg.app.timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise SettingsError("请选择有效的时区，例如 Asia/Shanghai 或 America/New_York。", "app.timezone") from None
    if not isinstance(schedule.enabled, bool) or not isinstance(schedule.handoff_confirmed, bool):
        raise SettingsError("自动更新开关格式不正确。")
    if not isinstance(schedule.time, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', schedule.time):
        raise SettingsError("请选择有效的更新时间。", "schedule.time")
    if schedule.owner not in ('application', 'external'):
        raise SettingsError("调度管理方式不正确。")
    if not isinstance(schedule.groups, list) or not all(isinstance(g,str) for g in schedule.groups):
        raise SettingsError("请选择研究方向。")
    if schedule.enabled and (schedule.owner != 'application' or not schedule.handoff_confirmed):
        raise SettingsError("请先让部署代理停用原有系统定时任务，并确认由应用接管，再启用自动更新。")
    known = {g.slug for g in rank.load_groups(cfg)}
    if set(schedule.groups) - known:
        raise SettingsError("选中的研究方向已变化，请重新选择。", "schedule.groups")


def connect(cfg):
    return db.Database(cfg.db_file).connect()


def occurrence(day, wall_time: str, zone: ZoneInfo) -> datetime:
    hour, minute = map(int, wall_time.split(':'))
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
    # A missing DST wall time rolls forward through the gap; an ambiguous time
    # uses the first occurrence. A local-date primary key prevents a second run.
    return local.astimezone(timezone.utc)


def due(cfg, now=None):
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(ZoneInfo(cfg.app.timezone)).date()
    when = occurrence(day, cfg.schedule.time, ZoneInfo(cfg.app.timezone))
    return day.isoformat(), when


def status(cfg, now=None):
    now = now or datetime.now(timezone.utc)
    conn = connect(cfg)
    try:
        last = conn.execute('SELECT * FROM scheduled_run ORDER BY started_at DESC LIMIT 1').fetchone()
        worker = conn.execute('SELECT * FROM scheduler_worker WHERE id=1').fetchone()
        slot, when = due(cfg, now)
        exists = conn.execute('SELECT 1 FROM scheduled_run WHERE slot=?', (slot,)).fetchone()
        if exists:
            when = occurrence(now.astimezone(ZoneInfo(cfg.app.timezone)).date() + timedelta(days=1),
                              cfg.schedule.time, ZoneInfo(cfg.app.timezone))
        alive = bool(worker and (now - datetime.fromisoformat(worker['heartbeat'])).total_seconds() < 120)
        return {'last': dict(last) if last else None, 'worker_alive': alive,
                'worker_message': worker['message'] if worker else '',
                'next': when.astimezone(ZoneInfo(cfg.app.timezone)).isoformat(timespec='minutes') if cfg.schedule.enabled else None,
                'overdue': bool(cfg.schedule.enabled and not exists and when < now)}
    finally:
        conn.close()


def run_selected(cfg):
    groups = [g for g in rank.load_groups(cfg) if g.enabled and
              (not cfg.schedule.groups or g.slug in cfg.schedule.groups)]
    if not groups:
        raise SettingsError("没有可更新的研究方向，请先添加或恢复一个方向。")
    execution.check_groups(cfg, 'all', groups)
    with credentials.snapshot(cfg):
        results = []
        if cfg.sources.xmol_enabled:
            results.append(pipeline.ingest_mail(cfg, verbose=False))
        for group in groups:
            results.append(pipeline.ingest_keyword_search(cfg, group=group, verbose=False))
        results.append(enrich.run(cfg, verbose=False))
        for group in groups:
            results.append(rank.run(cfg, days=cfg.app.pipeline_window_days, group=group, verbose=False))
        results.append(summarize.run(cfg, days=cfg.app.pipeline_window_days,
                                     selected_groups=groups, verbose=False))
        if any(result.get('errors',0) for result in results):
            raise SettingsError("部分阶段失败，请查看统计页运行记录。")


def tick(cfg, now=None, *, runner=None):
    """Check and claim today's occurrence under the existing pipeline lock."""
    validate(cfg)
    now = now or datetime.now(timezone.utc)
    conn = connect(cfg)
    try:
        conn.execute("INSERT OR REPLACE INTO scheduler_worker VALUES (1, ?, '')", (now.isoformat(),))
        conn.commit()
    finally:
        conn.close()
    if not cfg.schedule.enabled:
        return 'disabled'
    slot, when = due(cfg, now)
    if now < when:
        return 'waiting'
    try:
        with single_instance(cfg.db_file.parent / 'litradar.lock'):
            conn = connect(cfg)
            try:
                cursor = conn.execute('INSERT OR IGNORE INTO scheduled_run (slot,scheduled_at,started_at,status) VALUES (?,?,?,?)',
                    (slot, when.isoformat(), now.isoformat(), 'running'))
                conn.commit()
                if cursor.rowcount == 0:
                    return 'already_ran'
                outcome, message = 'ok', '更新完成。'
                try:
                    (runner or run_selected)(cfg)
                except (SettingsError, execution.ExecutionLimit) as error:
                    outcome, message = 'error', str(error)
                except Exception:
                    outcome, message = 'error', '更新未完成，请查看运行记录并检查连接状态。'
                conn.execute('UPDATE scheduled_run SET finished_at=?,status=?,message=? WHERE slot=?',
                    (datetime.now(timezone.utc).isoformat(), outcome, message, slot))
                conn.commit()
                return outcome
            finally:
                conn.close()
    except AlreadyRunning:
        return 'busy'  # no claim: retry after the current operation finishes


def heartbeat(cfg, stop, interval=30):
    """A long model call must not make a healthy worker appear offline."""
    while not stop.is_set():
        conn = connect(cfg)
        try:
            conn.execute("INSERT INTO scheduler_worker VALUES (1, ?, '') "
                         "ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat",
                         (datetime.now(timezone.utc).isoformat(),))
            conn.commit()
        finally:
            conn.close()
        stop.wait(interval)


def serve(config_path=None, stop=None):
    cfg = load_config(config_path)
    try:
        with single_instance(cfg.db_file.parent / 'litradar-scheduler.lock'):
            conn = connect(cfg)
            try:
                conn.execute("UPDATE scheduled_run SET status='interrupted', message='上次进程中断；为避免重复调用，当天不会自动重试。请检查运行记录后手动更新。' WHERE status='running'")
                conn.commit()
            finally:
                conn.close()
            heartbeat_stop = threading.Event()
            worker = threading.Thread(target=heartbeat, args=(cfg, heartbeat_stop), daemon=True)
            worker.start()
            try:
                while stop is None or not stop.is_set():
                    try:
                        fresh = load_config(config_path)
                        if fresh.db_file != cfg.db_file:
                            raise SettingsError("数据库位置已变化，请通过部署流程重启调度服务。")
                        tick(fresh)
                    except Exception:
                        conn = connect(cfg)
                        try:
                            conn.execute("INSERT OR REPLACE INTO scheduler_worker VALUES (1, ?, ?)",
                                (datetime.now(timezone.utc).isoformat(), '调度设置无效，请检查设置或由部署代理查看本机日志。'))
                            conn.commit()
                        finally:
                            conn.close()
                    if stop is None:
                        time.sleep(30)
                    elif stop.wait(30):
                        break
            finally:
                heartbeat_stop.set()
                worker.join(timeout=5)
    except (AlreadyRunning, KeyboardInterrupt):
        return  # another dedicated worker already owns this database
