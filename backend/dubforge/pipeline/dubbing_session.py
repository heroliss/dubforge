"""逐句合成与正式步骤共用项目占用保护，避免并发覆盖音频与参数。"""
from contextlib import contextmanager
from threading import RLock
from dubforge.domain import crud
from dubforge.errors import DubForgeError

gate = RLock()
_active = {}


def active_line(uid):
    return status(uid)['active_line']


def status(uid):
    """一次读取句号与进度，避免切换句子时混入上一句的进度。"""
    with gate:
        return dict(_active.get(uid, {'active_line': None, 'active_progress': None}))


def check_idle(uid):
    if uid in _active:
        raise DubForgeError('本项目正在合成配音，请等当前句完成后再操作', code='PIPELINE_BUSY')


@contextmanager
def edit(project, *, paths=None):
    """短暂编辑不显示为合成中，避免轮询导致播放器被卸载和复位。"""
    from dubforge import runtime_edit
    with runtime_edit.editing('project:' + project.uid, paths=paths):
        yield


@contextmanager
def use(project, line_id, *, from_pipeline=False):
    """正式步骤已经持有运行资格，与逐句合成共用当前句和进度记录。"""
    session = {'active_line': line_id, 'active_progress': None}
    from dubforge.runner import runner
    from dubforge import runtime_edit
    with runner._start_lock, gate:
        check_idle(project.uid)
        if not from_pipeline:
            runtime_edit.check_idle(project.uid)
        _active[project.uid] = session

    def progress(value, *, attempt=None, factor=None):
        with gate:
            if _active.get(project.uid) is session:
                session['active_progress'] = value
                if attempt is not None:
                    session.update(active_attempt=attempt, active_factor=factor)

    try:
        yield progress
    finally:
        with gate:
            _active.pop(project.uid, None)
