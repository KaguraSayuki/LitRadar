"""Manual runs persist progress while their worker holds the shared pipeline lock."""
from __future__ import annotations

import copy
import json
import re
import threading
import time
from contextlib import ExitStack

from .. import progress
from ..lock import AlreadyRunning, single_instance
from ..settings import SettingsError, atomic_write

LABELS = {'mail': '解析邮件', 'search': '关键词检索', 'enrich': '补全文献',
          'rank': '排序', 'summarize': '摘要'}


class JobStore:
    def __init__(self, cfg):
        self.path = cfg.db_file.with_name(cfg.db_file.name + '.web-jobs.json')
        self.metadata_lock = self.path.with_suffix('.lock')
        self.pipeline_lock = cfg.db_file.parent / 'litradar.lock'

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'latest': None, 'jobs': {}}
        except (ValueError, OSError):
            raise SettingsError('无法读取运行进度，请检查数据目录。已有文献未受影响。', status=500) from None

    def _write(self, data):
        atomic_write(self.path, json.dumps(data, ensure_ascii=False).encode(), backup=False)

    def _change(self, job_id, change):
        with single_instance(self.metadata_lock, blocking=True):
            data = self._read()
            state = data['jobs'][job_id]
            change(state)
            state['updated_at'] = time.time()
            self._write(data)
            return copy.deepcopy(state)

    def snapshot(self, job_id=None):
        with single_instance(self.metadata_lock, blocking=True):
            data = self._read()
            state = copy.deepcopy(data['jobs'].get(job_id or data['latest']))
        if state and state['status'] == 'running':
            # A terminated worker loses the OS lock. Never restart the job implicitly.
            try:
                with single_instance(self.pipeline_lock):
                    def interrupted(current):
                        if current['status'] == 'running':
                            current.update(status='interrupted', finished_at=time.time(),
                                message='任务进程已中断，已完成的数据保留。检查运行记录后可重新运行。')
                            for step in current['steps']:
                                if step['status'] == 'running':
                                    step['status'] = 'interrupted'
                    state = self._change(state['id'], interrupted)
            except AlreadyRunning:
                pass
        return state

    def start(self, job_id, stage, group, days, operation, check):
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', job_id):
            raise SettingsError('运行请求标识无效，请刷新页面后重试。')
        def same_request(previous):
            if (previous['stage'], previous['group_slug'], previous['days']) != (stage, group.slug if group else None, days):
                raise SettingsError('这个运行请求已用于其他操作，请刷新页面。', status=409)
            return previous
        previous = self.snapshot(job_id)
        if previous:
            return same_request(previous)
        stack = ExitStack()
        try:
            stack.enter_context(single_instance(self.pipeline_lock))
            now = time.time()
            plan = list(LABELS) if stage == 'all' else [stage]
            state = {'id': job_id, 'stage': stage, 'status': 'running',
                'group_slug': group.slug if group else None,
                'group_name': group.name if group and stage not in ('mail', 'enrich') else '全部方向',
                'days': days, 'started_at': now, 'updated_at': now, 'finished_at': None,
                'message': '任务已启动', 'current': None, 'total': None,
                'steps': [{'key': key, 'label': LABELS[key], 'status': 'waiting', 'message': ''} for key in plan]}
            with single_instance(self.metadata_lock, blocking=True):
                data = self._read()
                # A concurrent duplicate may have completed before the pipeline lock was obtained.
                if job_id in data['jobs']:
                    stack.close()
                    return same_request(copy.deepcopy(data['jobs'][job_id]))
                check()  # Limits are checked under the same lock as actual execution.
                for old in list(data['jobs'])[:-19]:
                    del data['jobs'][old]
                data['jobs'][job_id] = state
                data['latest'] = job_id
                self._write(data)
            worker = threading.Thread(target=self._run, args=(job_id, operation, stack), daemon=True)
            worker.start()
            return copy.deepcopy(state)
        except AlreadyRunning:
            stack.close()
            # A duplicate may arrive while the first request is starting its worker.
            previous = self.snapshot(job_id)
            if previous:
                return same_request(previous)
            raise
        except Exception:
            stack.close()
            raise

    def _run(self, job_id, operation, stack):
        def update(event):
            def change(state):
                step = next((step for step in state['steps'] if step['key'] == event.get('stage')), None)
                if not step:
                    return
                if event['kind'] == 'stage':
                    step['status'] = event['status']
                    step['message'] = event.get('message', '')
                    state.update(current=None, total=None)
                state['message'] = event.get('message') or f"正在{step['label']}"
                if event['kind'] == 'progress':
                    state.update(current=event.get('current'), total=event.get('total'))
                    step['message'] = event['message']
            self._change(job_id, change)
        try:
            with progress.observe(update):
                result = operation()
            partial = progress.has_errors(result)
            self._change(job_id, lambda state: state.update(
                status='partial' if partial else 'completed', finished_at=time.time(),
                message='运行结束，部分条目未完成。请查看各阶段结果和运行记录。' if partial else '运行完成，可返回文献列表查看结果。'))
        except Exception:
            self._change(job_id, lambda state: state.update(status='failed', finished_at=time.time(),
                message='运行未完成，已完成的数据保留。请查看运行记录后重试。'))
        finally:
            stack.close()
