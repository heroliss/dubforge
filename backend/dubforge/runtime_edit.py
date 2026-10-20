"""运行期间的编辑政策；保存与启动共用短锁，网络/制作等待不持编辑锁。"""
import json
import threading
from contextlib import contextmanager

from sqlmodel import Session, select, or_

from dubforge.db import get_engine
from dubforge.errors import DubForgeError

DISPLAY_ONLY_PATHS = frozenset({'ui.llm_stream.reference_distance_chars'})
_owner = threading.local()


def _runtime_next_boundary_path(path: str) -> bool:
    """返回只在下一次请求/步骤边界生效的连接或时限字段。

    步骤执行配置在进入 ``running`` 时已复制到 ``StepContext``。这些字段
    不改变当前上下文，也不改变产物内容指纹；继续把它们锁住会让用户无法
    在长配音期间为下一次运行准备更长的兜底时限。当前步骤中的后续单句/请求
    仍使用既有快照；项目/全局写入仍会经过
    同一启动锁，当前请求不会被热切换。
    """
    if path == 'llm.timeout':
        return True
    try:
        from dubforge.pipeline.step_params import RUNTIME_PARAM_PATHS
    except ImportError:
        RUNTIME_PARAM_PATHS = frozenset()
    return path in RUNTIME_PARAM_PATHS or path == 'runner.step_timeout_seconds'


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
    from dubforge import project_batches
    if project_batches.owns(uid) or project_batches.child_owned(uid):
        return True
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


def _step_lock_paths(step_key):
    """返回步骤实际可能消费的设置路径。

    配置保存是点分路径，而步骤注册表用前缀声明依赖。这里展开为 schema
    中的公开字段，保留前缀本身以兼容全局设置中 ``llm`` 这类整段更新。
    """
    if step_key == 'publish_copy':
        # 候选生成冻结当前发布表单；制作步骤仍可调整下一次使用的参数。
        return {'publish'}
    from dubforge.pipeline.registry import STEP_MAP
    from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA

    paths = set()
    definition = STEP_MAP.get(step_key)
    if definition:
        paths.update(definition.cfg_keys)
    for field in STEP_PARAM_SCHEMA.get(step_key, ()):
        paths.add(field.path)
    if step_key != 'tts' and 'tts.placement' in paths:
        # 展开真实排布字段，不能把只在 TTS 使用的缩时修复也显示为已冻结。
        paths.remove('tts.placement')
        paths.update(field.path for field in STEP_PARAM_SCHEMA['tts']
                     if field.path.startswith('tts.placement.')
                     and field.path != 'tts.placement.auto_repair')
    # 内容筛查和评论不是完全独立的 StepRun，但它们的设置会被这些步骤消费。
    if step_key in {'comments', 'subtitles', 'assemble'}:
        paths.add('content_review')
        paths.update(field.path for field in STEP_PARAM_SCHEMA.get('content_review', ()))
    if step_key in {'comments', 'assemble'}:
        paths.add('comments')
        paths.update(field.path for field in STEP_PARAM_SCHEMA.get('comments', ()))
        paths.update({'comments', 'llm'})
    if step_key == 'assemble':
        # 版本清单和发布分P选择决定实际成片输入，虽不在 StepDef.cfg_keys
        # 中，也必须随合成步骤一起冻结。
        paths.update({'assembly.versions', 'publish.version_ids', 'publish.part_titles'})
    # 这些运行器参数也会影响当前步骤的执行合同；并发上限本身只影响
    # 排队调度，因此不在这里冻结。
    paths.update({'runner.step_timeout_seconds', f'runner.step_timeout_seconds.{step_key}'})
    if step_key in {'preprocess', 'transcribe', 'tts'}:
        paths.update({'runner.auto_tts_vram', 'runner.gpu_free_reserve_mb'})
    if step_key == 'tts':
        paths.add('runner.tts_start_timeout')
    if step_key in {'tts', 'subtitles', 'assemble'}:
        paths.add('playback_timing_edits')
    if step_key == 'translate':
        # 翻译背景资料保存在项目参数中，但在执行时同样是翻译输入。
        paths.add('translation_context')
    return paths


def _paths_for_step(path, step_key):
    if step_key == 'project_batch_goal':
        from dubforge.project_batches import GOAL_PATHS
        return any(path == key or path.startswith(key + '.') or key.startswith(path + '.')
                   for key in GOAL_PATHS)
    if path == 'tts_lines.live':
        # 唯一例外是明确的逐句声音请求；不能在上游文本/参考正在替换时排队。
        # TTS 由 live_dubbing 的当前任务、停止及最终关闭边界进一步核验。
        return step_key in {'download', 'preprocess', 'transcribe', 'split', 'translate'}
    # 缩时修复只在配音执行中消费；字幕和成片仅使用既有声音与排布结果。
    if path == 'tts.placement.auto_repair' and step_key != 'tts':
        return False
    # 连接地址、单次请求时限和步骤兜底只在新的请求/步骤快照中生效；
    # 当前运行继续使用已经冻结的值，因此允许在长任务期间保存。
    if isinstance(path, str) and _runtime_next_boundary_path(path):
        return False
    return any(path == candidate or path.startswith(candidate + '.')
               or candidate.startswith(path + '.')
               or candidate == path.split('.', 1)[0]
               for candidate in _step_lock_paths(step_key))


def _locked_step_view(step_key, status='running'):
    from dubforge.pipeline.registry import STEP_MAP
    return dict(step_key=step_key, label='发布文案生成' if step_key == 'publish_copy'
                else STEP_MAP[step_key].label if step_key in STEP_MAP else step_key,
                status=status, locked_paths=sorted(_step_lock_paths(step_key)))


def _report(session, uids, *, allow_owned=False, retry_step=None, paths=None, strict=False):
    from dubforge import video_info, workflows, release_batches
    from dubforge.domain.models import Project, StepRun
    from dubforge.domain import crud
    from dubforge.pipeline import dubbing_session
    from dubforge.publishing import service, studio, project_copy
    from dubforge.publishing.models import Publication, PublishChange, PublicationUpdate
    from dubforge.runner import runner
    from dubforge.workflow_models import WorkflowRun
    if allow_owned:
        uids = [uid for uid in uids if not _owned(uid)]
    if not uids:
        return dict(locked=False, reason='', project_uids=[], resource_locked=False,
                    resource_reason='', resource_project_uids=[], running_steps=[],
                    locked_paths=[], locked_paths_by_project={})
    projects = session.exec(select(Project.id, Project.uid).where(Project.uid.in_(uids))).all()
    ids = dict(projects)
    known_uids = set(ids.values())
    reasons = {}

    def busy(uid, reason):
        reasons.setdefault(uid, reason)

    # 排队中的步骤尚未绑定执行配置，允许修改；只有已经开始运行的步骤锁住
    # 自己实际消费的设置。其它资源（发布、导入、归档等）仍由原有门禁处理。
    active_rows = session.exec(select(StepRun.project_id, StepRun.step_key, StepRun.status).where(
        StepRun.project_id.in_(ids), StepRun.status.in_({'queued', 'running', 'stopping', 'cancelling'}))).all() if ids else []
    active = {}
    occupied = set()
    for project_id, step_key, status in active_rows:
        uid = ids[project_id]
        if status == 'queued':
            # 排队任务可以改设置，但结构性操作（绑定/归档/改名）仍需等
            # 它完成或取消，避免执行计划在队列中途被换掉。
            occupied.add(uid)
            # ``editability`` exposes this as a separate resource lock.  Do
            # not add a step row here: queued work has not captured a settings
            # snapshot yet, so its fields must remain editable.
            busy(uid, '步骤已排队，项目级操作暂不能进行')
            continue
        active.setdefault(uid, []).append(_locked_step_view(step_key, status))
        # Keep project-level management operations blocked while the step is
        # running or stopping, while ``locked`` below remains field-scoped.
        # Consumers that only need the resource gate should not have to infer
        # it from the detailed ``running_steps`` list.
        label = active[uid][-1]['label']
        busy(uid, f'「{label}」正在运行，项目级操作暂不能进行')
    # 状态落盘和线程收尾之间可能有极短窗口；内存中的 current_jobs/ctxs
    # 仍代表真正正在执行的步骤，不能因数据库先落 failed 就放开其设置。
    with runner._lock:
        live_keys = set(runner._current_jobs) | set(runner._ctxs)
    known = {(uid, row['step_key']) for uid, rows in active.items() for row in rows}
    for project_id, step_key in live_keys:
        uid = ids.get(project_id)
        if not uid or (uid, step_key) in known:
            continue
        # _current_jobs 在入队时就登记，_ctxs 也可能先于资源槽位创建；
        # queued 仍是可编辑边界，不能把等待中的任务误报为运行中。
        live_step = crud.get_step(project_id, step_key)
        if live_step is not None and live_step.status == 'queued':
            continue
        if uid:
            active.setdefault(uid, []).append(_locked_step_view(step_key, 'running'))
    # 逐句试听/合成不一定创建 StepRun，但它仍会读写同一套 TTS 产物；
    # 视频资料刷新也会使用下载设置。把这些短生命周期操作映射到对应步骤，
    # 这样保存其它步骤的设置仍可继续，而当前步骤的字段会正确冻结。
    for uid in uids:
        project_id = next((ident for ident, project_uid in ids.items() if project_uid == uid), None)
        if project_id is None:
            continue
        if uid in dubbing_session._active and not any(
                row['step_key'] == 'tts' for row in active.get(uid, ())):
            active.setdefault(uid, []).append(_locked_step_view('tts', 'running'))
        if uid in video_info._active and not any(
                row['step_key'] == 'download' for row in active.get(uid, ())):
            active.setdefault(uid, []).append(_locked_step_view('download', 'running'))
    # 全流程的 comments 阶段没有独立 StepRun；以流程游标补上一条当前步骤锁。
    # 有对应 StepRun 时以 Runner 的真实状态为准，queued 仍允许修改配置。
    for workflow in session.exec(select(WorkflowRun.project_uid, WorkflowRun.status,
                                        WorkflowRun.cursor, WorkflowRun.plan).where(
                                            WorkflowRun.project_uid.in_(uids),
                                            WorkflowRun.status == 'running')).all():
        uid, _status, cursor, raw_plan = workflow
        try:
            workflow_plan = json.loads(raw_plan or '{}')
            # A partially written/legacy plan must not make the editability
            # probe fail with a 500.  The persisted workflow resource is still
            # handled by the status query below; only the detailed step lock
            # is unavailable for malformed plans.
            steps = (workflow_plan.get('steps') or []) if isinstance(workflow_plan, dict) else []
            step_key = steps[int(cursor)] if 0 <= int(cursor) < len(steps) else None
        except (TypeError, ValueError, json.JSONDecodeError, KeyError, IndexError):
            step_key = None
        if not step_key or step_key in {'publish', 'publication'}:
            continue
        project_id = next((ident for ident, project_uid in ids.items() if project_uid == uid), None)
        existing = crud.get_step(project_id, step_key) if project_id is not None else None
        # StepRun 的运行/停止状态已经由上面的真实活动行覆盖；终态表示该
        # 阶段已完成，不能因为流程游标尚未来得及前移而继续冻结设置。
        if existing is not None:
            continue
        row = _locked_step_view(step_key, 'running')
        if not any(item['step_key'] == step_key for item in active.get(uid, ())):
            active.setdefault(uid, []).append(row)
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
        if uid in release_batches._leases:
            busy(uid, release_batches._lease_error(uid, release_batches._leases[uid]))
        if uid in dubbing_session._active:
            busy(uid, '单句配音正在执行或收尾')
        if uid in video_info._active:
            busy(uid, '视频资料或评论任务正在执行或收尾')
        if uid in workflows._leases:
            busy(uid, '自动流程正在执行或停止中')
        if uid in project_copy._suggesting:
            busy(uid, '项目发布文案正在生成')
            if not any(row['step_key'] == 'publish_copy' for row in active.get(uid, ())):
                active.setdefault(uid, []).append(_locked_step_view('publish_copy'))
    runs = session.exec(select(WorkflowRun.id, WorkflowRun.project_uid, WorkflowRun.status).where(
        WorkflowRun.project_uid.in_(uids), or_(WorkflowRun.status.in_({'queued', 'running', 'stopping'}),
                                             WorkflowRun.id.in_(tuple(workflows._running))))).all()
    for run in runs:
        if (run.status in {'queued', 'running', 'stopping'}
                or (run.id in workflows._running and run.status != 'monitoring')):
            busy(run.project_uid, '自动流程正在执行或后台收尾')
    # 批次状态先落盘再设置内存租约；读取数据库占用可覆盖这段极短窗口，
    # 也能在多请求并发时避免项目入口先看到可编辑后立即被批次冻结。
    from dubforge.release_batch_models import ReleaseBatch
    active_batch_ids = tuple(release_batches._running)
    batch_query = select(ReleaseBatch).where(or_(
        ReleaseBatch.status.in_(release_batches.ACTIVE),
        ReleaseBatch.id.in_(active_batch_ids),
    ))
    for batch in session.exec(batch_query).all():
        try:
            batch_items = json.loads(batch.items)
        except (TypeError, ValueError):
            batch_items = []
        for index, item in enumerate(batch_items):
            if not isinstance(item, dict) or item.get('status') == 'done':
                continue
            uid = item.get('project_uid')
            if uid not in known_uids:
                continue
            stage = '等待前项完成' if item.get('status') == 'queued' else item.get('phase') or batch.phase or '正在处理'
            busy(uid, f'项目已锁定在顺序发布批次中（第 {index + 1}/{len(batch_items)} 项，{stage}），'
                 '暂不能单独运行步骤、自动流程或修改制作资料')
    # 混合批次预留尚未启动的项目；普通参数仍可保存，目标和结构操作冻结。
    from dubforge import project_batches
    from dubforge.project_batch_models import ProjectBatch
    for batch in session.exec(select(ProjectBatch).where(ProjectBatch.status.in_(project_batches.ACTIVE))).all():
        for group in project_batches.stored_groups(batch.groups):
            if group.get('status') not in project_batches.ACTIVE:
                continue
            for uid in group['project_uids']:
                if uid not in known_uids or project_batches.owns(uid) or project_batches.child_owned(uid):
                    continue
                busy(uid, f'项目已加入批量运行分组「{group["title"]}」，请先暂停本组再变更执行目标')
                active.setdefault(uid, []).append(dict(step_key='project_batch_goal', label='批量任务目标',
                    status='running', locked_paths=sorted(project_batches.GOAL_PATHS)))
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
            if not any(item['step_key'] == 'publish_copy' for item in active.get(row.project_uid, ())):
                active.setdefault(row.project_uid, []).append(_locked_step_view('publish_copy'))
    changes = session.exec(select(PublishChange.id, Publication.project_uid).join(
        Publication, Publication.id == PublishChange.publication_id).where(
        Publication.project_uid.in_(uids), or_(PublishChange.status == 'committing',
                                             PublishChange.id.in_(tuple(studio._inflight))))).all()
    for _, uid in changes:
        busy(uid, '稿件修改正在提交或收尾')
    original_updates = session.exec(select(PublicationUpdate).where(
        PublicationUpdate.project_uid.in_(uids), PublicationUpdate.status.in_({'queued', 'uploading', 'submitting'}))).all()
    for update in original_updates:
        busy(update.project_uid, '原稿更新正在上传或提交，新成片已冻结')
    # 配置写入的细粒度门禁：只有提交字段与活动步骤相交才拒绝。
    locked_paths = sorted({path for rows in active.values() for row in rows for path in row['locked_paths']})
    # 在添加“本次字段与运行步骤相交”的临时原因前保存项目级资源占用；
    # 否则单个字段被步骤冻结也会误报成整个项目资源锁。
    resource_blocked = sorted(reasons)
    path_blocked = []
    if paths is not None:
        for uid, rows in active.items():
            for row in rows:
                if any(_paths_for_step(path, row['step_key']) for path in paths):
                    path_blocked.append(uid)
                    reasons.setdefault(uid, f'「{row["label"]}」正在运行，相关设置已冻结')
    elif strict:
        # 非设置类写操作（例如改名、归档、模板绑定）继续使用项目级占用门禁。
        path_blocked = sorted(set(active) | occupied | set(reasons))
    # ``locked`` 表示设置字段维度。归档、删除、模板绑定和启动其它任务等
    # 项目级操作在写入时使用 ``strict=True``；状态查询则单独返回资源维度，
    # 使运行中的步骤不会把无关设置整体置灰，同时仍能说明项目级操作为何不可用。
    blocked = sorted(set(path_blocked) | (set(reasons) if paths is None and strict else set()))
    reason = ('相关项目' if len(blocked) > 1 else '本项目') + '有任务尚未结束，暂不能执行此操作。'
    if blocked:
        reason += '（' + '；'.join(dict.fromkeys(reasons.get(uid, '步骤正在运行，相关写入暂不能进行') for uid in blocked)) + '）'
    return dict(locked=bool(blocked), reason=reason if blocked else '', project_uids=blocked,
                resource_locked=bool(resource_blocked),
                resource_reason=(
                    (('相关项目' if len(resource_blocked) > 1 else '本项目')
                     + '有项目级操作尚未结束。'
                     + '（' + '；'.join(dict.fromkeys(reasons[uid] for uid in resource_blocked)) + '）')
                    if resource_blocked else ''),
                resource_project_uids=resource_blocked,
                running_steps=[dict(project_uid=uid, **row) for uid, rows in active.items() for row in rows],
                locked_paths=locked_paths,
                locked_paths_by_project={uid: sorted({path for row in rows for path in row['locked_paths']})
                                         for uid, rows in active.items()})


def editability(key):
    from dubforge.pipeline import dubbing_session
    from dubforge.runner import runner
    with runner._start_lock, dubbing_session.gate, Session(get_engine()) as session:
        uids = _projects(session, key)
        if key.startswith('project:'):
            from dubforge.domain.models import Project
            archived_at = session.exec(select(Project.archived_at).where(Project.uid == key[8:])).first()
            if archived_at is not None:
                return dict(locked=True, reason='本项目已归档，请先恢复项目后再修改或运行。', project_uids=uids,
                            resource_locked=True, resource_reason='本项目已归档，请先恢复项目后再修改或运行。',
                            resource_project_uids=uids, running_steps=[], locked_paths=[],
                            locked_paths_by_project={})
        # 此接口供表单轮询。运行中的其它步骤不应把整个设置页变灰，
        # 因此 locked 只表示归档/全局资源不可用，字段锁定见 locked_paths。
        return _report(session, uids, strict=False)


def assert_editable(key, *, allow_owned=False, retry_step=None, allow_archived=False, paths=None):
    """调用方须持启动锁；授权内部写入只豁免当前线程实际拥有的本项目。"""
    if key.startswith('project:') and not allow_archived:
        from dubforge.project_archive import assert_active
        assert_active(key[8:])
    with Session(get_engine()) as session:
        report = _report(session, _projects(session, key),
                         allow_owned=allow_owned and key.startswith('project:'),
                         retry_step=retry_step if key.startswith('project:') else None,
                         paths=set(paths) if paths is not None else None,
                         strict=paths is None)
    if report['locked']:
        raise RuntimeEditError(report['reason'], detail=report)
    return report


def check_idle(uid, *, allow_owned=False, retry_step=None, paths=None):
    return assert_editable('project:' + uid, allow_owned=allow_owned, retry_step=retry_step, paths=paths)


@contextmanager
def editing(key, *, allow_owned=False, allow_archived=False, paths=None):
    from dubforge.pipeline import dubbing_session
    from dubforge.runner import runner
    with runner._start_lock, dubbing_session.gate:
        assert_editable(key, allow_owned=allow_owned, allow_archived=allow_archived,
                        paths=paths)
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
