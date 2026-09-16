"""Optional structured progress from pipeline code, isolated to the current task."""
from contextlib import contextmanager
from contextvars import ContextVar

_observer = ContextVar('litradar_progress_observer', default=None)
_stage = ContextVar('litradar_progress_stage', default=None)


@contextmanager
def observe(callback):
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


def emit(event):
    callback = _observer.get()
    if callback:
        callback(event)


def report(message: str, current=None, total=None):
    emit({'kind': 'progress', 'stage': _stage.get(), 'message': message,
          'current': current, 'total': total})


def has_errors(value) -> bool:
    if not isinstance(value, dict):
        return False
    return any((key in ('error', 'errors', 'failed', 'brief_failed', 'deep_failed',
                       'relevance_failed', 'llm_failed', 'jr_failed') and bool(item))
               or (isinstance(item, dict) and has_errors(item)) for key, item in value.items())


def summary(result) -> str:
    if not isinstance(result, dict):
        return '处理完成'
    labels = {'messages': '封邮件', 'new': '篇新增', 'updated': '篇更新', 'scored': '篇评分',
              'llm_scored': '篇 AI 评分', 'deep': '篇深度摘要', 'brief': '篇简要摘要',
              'relevance': '条方向说明', 'enriched': '篇补全', 'errors': '项错误',
              'brief_failed': '篇简要摘要失败', 'deep_failed': '篇深度摘要失败',
              'llm_failed': '篇 AI 评分失败', 'relevance_failed': '条方向说明失败',
              'failed': '篇未补全', 'jr_failed': '项期刊查询失败'}
    if 'llm_new' in result:
        labels.pop('scored')
        labels.pop('llm_scored')
        labels.pop('llm_failed')
        labels.update(llm_new='篇首次 AI 评分', llm_refreshed='篇历史重评',
                      llm_reused='篇复用评分', llm_pending='篇待完成')
    parts = [f'{result[key]} {label}' for key, label in labels.items()
             if type(result.get(key)) is int and result[key] > 0]
    if parts:
        return ' · '.join(parts)
    return '已跳过（未启用或无需处理）' if result.get('skipped') else '处理完成'


def run_stage(name, operation):
    token = _stage.set(name)
    try:
        emit({'kind': 'stage', 'stage': name, 'status': 'running'})
        result = operation()
        status = 'partial' if has_errors(result) else 'completed'
        if isinstance(result, dict) and isinstance(result.get('skipped'), (str, bool)) and result['skipped']:
            status = 'skipped'
        emit({'kind': 'stage', 'stage': name, 'status': status, 'message': summary(result)})
        return result
    except Exception:
        emit({'kind': 'stage', 'stage': name, 'status': 'failed', 'message': '该阶段未完成，请查看运行记录。'})
        raise
    finally:
        _stage.reset(token)
