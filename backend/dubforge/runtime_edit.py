"""运行期间的编辑政策；保存与启动共用短锁，网络/制作等待不持编辑锁。"""
import threading
from contextlib import contextmanager

from sqlmodel import Session, select, or_

from dubforge.db import get_engine
from dubforge.errors import DubForgeError

DISPLAY_ONLY_PATHS = frozenset({'ui.llm_stream.reference_distance_chars'})
_owner = threading.local()


class RuntimeEditError(DubForgeError):
    code = 'CONFIGURATION_BUSY'
    suggestion = '请等待任务结束；停止后还需等待后台收尾，再修改设置或勾选项。'


def display_only(updates):
    """例外只限精确点分键；显示设置混入任何其他项时整批仍须检查。"""
    return isinstance(updates, dict) and bool(updates) and set(updates) <= DISPLAY_ONLY_PATHS


def _projects(session, key):
    from dubforge import configuration
    from dubforge.configuration_models import ConfigurationScope
    from dubforge.domain.models import Project
    if key.startswith('project:'):
        if not session.exec(select(Project.uid).where(Project.uid == key[8:])).first():
            raise configuration.ConfigurationError('项目不存在', code='PROJECT_NOT_FOUND')
        return [key[8:]]
    if key.startswith('series:'):
        if not session.exec(select(ConfigurationScope.key).where(ConfigurationScope.key == key)).first():
            raise configuration.ConfigurationError('系列模板不存在', code='CONFIGURATION_NOT_FOUND')
        bindings = session.exec(select(ConfigurationScope.key).where(ConfigurationScope.parent == key)).all()
        return sorted({name[8:] for name in bindings if name.startswith('project:')})
    if key == 'global':
        return sorted(session.exec(select(Project.uid)).all())
    raise configuration.ConfigurationError('设置范围无效')


def _owned(uid):
    from dubforge import workflows
    lease = workflows._leases.get(uid)
    if lease and getattr(workflows._owner, 'key', None) == lease:
        return True
    context = getattr(_owner, 'context', None)
    if context is None or context.project.uid != uid:
        return False
    from dubforge.runner import runner
    with runner._lock:
        key = (context.project.id, getattr(context, 'step_key', None))
        return runner._ctxs.get(key) is context and key in runner._current_jobs


def _report(session, uids, *, allow_owned=False, retry_step=None):
    from dubforge import video_info, workflows
    from dubforge.domain.models import Project, StepRun
    from dubforge.pipeline import dubbing_session
    from dubforge.publishing import service, studio, project_copy
    from dubforge.publishing.models import Publication, PublishChange
    from dubforge.runner import runner
    from dubforge.workflow_models import WorkflowRun
    if allow_owned:
        uids = [uid for uid in uids if not _owned(uid)]
    if not uids:
        return dict(locked=False, reason='', project_uids=[])
    projects = session.exec(select(Project.id, Project.uid).where(Project.uid.in_(uids))).all()
    ids = dict(projects)
    reasons = {}

    def busy(uid, reason):
        reasons.setdefault(uid, reason)

    steps = session.exec(select(StepRun.project_id).where(StepRun.project_id.in_(ids),
        StepRun.status.in_({'running', 'queued', 'stopping', 'cancelling'}))).all() if ids else []
    for project_id in steps:
        busy(ids[project_id], '制作任务正在排队、运行或停止中')
    # Step 状态可能先终结，线程还未清理；不能只看页面上的 failed/done。
    with runner._lock:
        pending = set(runner._current_jobs) | set(runner._ctxs) | set(runner._abandoned)
        retries = set(runner._retries)
    # 开始/重跑是运行控制，可替代本步等待计划；编辑不能借此豁免其它占用。
    pending.update(key for key in retries if not (retry_step is not None
                   and len(uids) == 1 and ids.get(key[0]) == uids[0] and key[1] == retry_step))
    for project_id, _ in pending:
        if project_id in ids:
            busy(ids[project_id], '制作任务仍在执行或后台收尾')
    for uid in uids:
        if uid in dubbing_session._active:
            busy(uid, '单句配音正在执行或收尾')
        if uid in video_info._active:
            busy(uid, '视频资料或评论任务正在执行或收尾')
        if uid in workflows._leases:
            busy(uid, '自动流程正在执行或停止中')
        if uid in project_copy._suggesting:
            busy(uid, '项目发布文案正在生成')
    runs = session.exec(select(WorkflowRun.id, WorkflowRun.project_uid, WorkflowRun.status).where(
        WorkflowRun.project_uid.in_(uids), or_(WorkflowRun.status.in_({'queued', 'running', 'stopping'}),
                                             WorkflowRun.id.in_(tuple(workflows._running))))).all()
    for run in runs:
        if (run.status in {'queued', 'running', 'stopping'}
                or (run.id in workflows._running and run.status != 'monitoring')):
            busy(run.project_uid, '自动流程正在执行或后台收尾')
    local_statuses = (service.ACTIVE - {'checking'}) | {'preparing', 'stopping'}
    publications = session.exec(select(Publication.id, Publication.project_uid, Publication.status).where(
        Publication.project_uid.in_(uids), or_(Publication.status.in_(local_statuses),
            Publication.id.in_(tuple(service._running)), Publication.id.in_(tuple(service._suggesting))))).all()
    for row in publications:
        # monitoring/submitted 是远程审核等待；checking 仅查询回执，无制作写入。
        if (row.status in local_statuses
                or (row.id in service._running and row.status != 'checking')):
            busy(row.project_uid, '上传、投稿或发布任务正在执行或收尾')
        if row.id in service._suggesting:
            busy(row.project_uid, '稿件发布文案正在生成')
    changes = session.exec(select(PublishChange.id, Publication.project_uid).join(
        Publication, Publication.id == PublishChange.publication_id).where(
        Publication.project_uid.in_(uids), or_(PublishChange.status == 'committing',
                                             PublishChange.id.in_(tuple(studio._inflight))))).all()
    for _, uid in changes:
        busy(uid, '稿件修改正在提交或收尾')
    blocked = sorted(reasons)
    reason = ('相关项目' if len(blocked) > 1 else '本项目') + '有任务尚未结束，暂不能修改设置或勾选项。'
    if blocked:
        reason += '（' + '；'.join(dict.fromkeys(reasons[uid] for uid in blocked)) + '）'
    return dict(locked=bool(blocked), reason=reason if blocked else '', project_uids=blocked)


def editability(key):
    from dubforge.pipeline import dubbing_session
    from dubforge.runner import runner
    with runner._start_lock, dubbing_session.gate, Session(get_engine()) as session:
        return _report(session, _projects(session, key))


def assert_editable(key, *, allow_owned=False, retry_step=None):
    """调用方须持启动锁；授权内部写入只豁免当前线程实际拥有的本项目。"""
    with Session(get_engine()) as session:
        report = _report(session, _projects(session, key),
                         allow_owned=allow_owned and key.startswith('project:'),
                         retry_step=retry_step if key.startswith('project:') else None)
    if report['locked']:
        raise RuntimeEditError(report['reason'], detail=report)
    return report


def check_idle(uid, *, allow_owned=False, retry_step=None):
    return assert_editable('project:' + uid, allow_owned=allow_owned, retry_step=retry_step)


@contextmanager
def editing(key, *, allow_owned=False):
    from dubforge.pipeline import dubbing_session
    from dubforge.runner import runner
    with runner._start_lock, dubbing_session.gate:
        assert_editable(key, allow_owned=allow_owned)
        yield


@contextmanager
def pipeline_writes(context):
    """仅实际 Runner 上下文可授权步骤内评论准备，不能由请求 body 授权。"""
    previous = getattr(_owner, 'context', None)
    _owner.context = context
    try:
        yield
    finally:
        _owner.context = previous
