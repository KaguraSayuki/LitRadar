"""Persistent calendar claims and policy across restarts, DST, and entrypoints."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from litradar.config import Config
from litradar import scheduler, execution, db
from litradar.lock import single_instance
from litradar.settings import SettingsError


@pytest.fixture
def cfg(tmp_path):
    cfg = Config()
    cfg.app.db_path = str(tmp_path/'test.db')
    cfg.app.timezone='America/New_York'
    cfg.schedule.enabled=True
    cfg.schedule.owner='application'
    cfg.schedule.handoff_confirmed=True
    return cfg


def test_claim_survives_new_connections_and_time_changes(cfg):
    calls=[]
    now=datetime(2026,9,14,15,tzinfo=timezone.utc)
    assert scheduler.tick(cfg,now,runner=lambda c:calls.append(1)) == 'ok'
    assert scheduler.tick(cfg,now,runner=lambda c:calls.append(2)) == 'already_ran'
    cfg.schedule.time='08:00'
    assert scheduler.tick(cfg,now,runner=lambda c:calls.append(3)) == 'already_ran'
    assert calls == [1]
    assert scheduler.status(cfg,now)['last']['status']=='ok'


def test_busy_pipeline_is_not_claimed_and_can_run_later(cfg):
    now=datetime(2026,9,14,15,tzinfo=timezone.utc)
    with single_instance(cfg.db_file.parent/'litradar.lock'):
        assert scheduler.tick(cfg,now,runner=lambda c:None)=='busy'
    assert scheduler.tick(cfg,now,runner=lambda c:None)=='ok'


def test_interrupted_or_failed_run_is_not_automatically_repeated(cfg):
    now=datetime(2026,9,14,15,tzinfo=timezone.utc)
    def fail(cfg): raise RuntimeError('provider URL with private token')
    assert scheduler.tick(cfg,now,runner=fail)=='error'
    assert 'private token' not in scheduler.status(cfg,now)['last']['message']
    assert scheduler.tick(cfg,now,runner=lambda c:None)=='already_ran'


def test_dst_gap_rolls_forward_and_fold_runs_once(cfg):
    zone=ZoneInfo('America/New_York')
    assert scheduler.occurrence(date(2026,3,8),'02:30',zone)==datetime(2026,3,8,7,30,tzinfo=timezone.utc)
    assert scheduler.occurrence(date(2026,11,1),'01:30',zone)==datetime(2026,11,1,5,30,tzinfo=timezone.utc)
    cfg.schedule.time='01:30'
    assert scheduler.tick(cfg,datetime(2026,11,1,5,31,tzinfo=timezone.utc),runner=lambda c:None)=='ok'
    assert scheduler.tick(cfg,datetime(2026,11,1,6,31,tzinfo=timezone.utc),runner=lambda c:None)=='already_ran'


def test_before_time_waits_and_external_owner_cannot_enable(cfg):
    assert scheduler.tick(cfg,datetime(2026,9,14,8,tzinfo=timezone.utc),runner=lambda c:None)=='waiting'
    cfg.schedule.handoff_confirmed=False
    with pytest.raises(SettingsError,match='接管'):
        scheduler.tick(cfg)


def test_limits_use_configured_calendar_not_machine_timezone(cfg):
    cfg.app.timezone='Pacific/Honolulu'
    cfg.admin.cooldown_seconds=0
    cfg.admin.daily_limit=1
    conn=db.Database(cfg.db_file).connect()
    # 02:00 UTC is still the previous calendar day in Honolulu.
    conn.execute("INSERT INTO run_log(stage,started_at,finished_at,status) VALUES('rank',?,?, 'ok')",
                 ('2026-09-14T02:00:00+00:00','2026-09-14T02:00:00+00:00'))
    conn.commit();conn.close()
    with pytest.raises(execution.ExecutionLimit):
        execution.check_limits(cfg,'rank',now=datetime(2026,9,14,6,tzinfo=timezone.utc))
    execution.check_limits(cfg,'rank',now=datetime(2026,9,14,12,tzinfo=timezone.utc))


def test_all_inherits_rank_guard_and_free_mail_stays_free(cfg):
    cfg.admin.guarded_stages=['rank']
    assert execution.guarded(cfg,'all')
    assert not execution.guarded(cfg,'mail')


def _competing_worker(cfg, barrier, queue):
    now = datetime(2026, 9, 14, 15, tzinfo=timezone.utc)
    def run(config):
        with (config.db_file.parent / 'calls.txt').open('a') as stream:
            stream.write('run\n')
    barrier.wait(timeout=10)
    queue.put(scheduler.tick(cfg, now, runner=run))


def test_two_processes_cannot_execute_the_same_calendar_slot(cfg):
    import multiprocessing
    conn = scheduler.connect(cfg)
    conn.close()
    context = multiprocessing.get_context('spawn')
    barrier, queue = context.Barrier(2), context.Queue()
    workers = [context.Process(target=_competing_worker, args=(cfg, barrier, queue)) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        outcomes = [queue.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        queue.close()
    assert outcomes.count('ok') == 1
    assert set(outcomes) <= {'ok', 'busy', 'already_ran'}
    assert (cfg.db_file.parent / 'calls.txt').read_text() == 'run\n'
    assert scheduler.tick(cfg, datetime(2026, 9, 14, 16, tzinfo=timezone.utc),
                          runner=lambda c: pytest.fail('must not run again')) == 'already_ran'


def test_selected_directions_share_one_summary_run(cfg, monkeypatch):
    cfg.interests_data = {'groups': [{'slug': name, 'name': name} for name in ('a', 'b', 'c')]}
    cfg.schedule.groups = ['a', 'c']
    calls = []
    def phase(name):
        def run(config, **kwargs):
            calls.append((name, kwargs))
            return {}
        return run
    monkeypatch.setattr(scheduler.execution, 'check_groups', lambda *a: None)
    monkeypatch.setattr(scheduler.pipeline, 'ingest_mail', phase('mail'))
    monkeypatch.setattr(scheduler.pipeline, 'ingest_keyword_search', phase('search'))
    monkeypatch.setattr(scheduler.enrich, 'run', phase('enrich'))
    monkeypatch.setattr(scheduler.rank, 'run', phase('rank'))
    monkeypatch.setattr(scheduler.summarize, 'run', phase('summary'))
    scheduler.run_selected(cfg)
    assert [name for name, _ in calls] == ['mail', 'search', 'search', 'enrich', 'rank', 'rank', 'summary']
    assert [group.slug for group in calls[-1][1]['selected_groups']] == ['a', 'c']
