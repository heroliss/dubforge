"""项目空间首屏：只读数据库与执行器内存，不把历史状态冒充当前产物核验。"""
import hashlib
import json
from types import SimpleNamespace

from sqlalchemy import case, func
from sqlmodel import select

from dubforge.domain import crud
from dubforge.domain.models import Project, StepRun
from dubforge.pipeline.registry import PIPELINE_KEYS, STEP_MAP
from dubforge.project_archive import ProjectArchiveError, archived_at_iso
from dubforge.publishing.models import Publication, PublicationUpdate
from dubforge.workflow_models import WorkflowRun


_STEP_ACTIVE = {'queued', 'running'}
_FLOW_ACTIVE = {'queued', 'running', 'stopping', 'monitoring'}
_PUB_ACTIVE = {'queued', 'uploading', 'submitting', 'joining', 'checking'}
_UPDATE_ACTIVE = {'queued', 'uploading', 'submitting', 'delivering'}
_COMMENT_ACTIONS = {'comments', 'filter_comments', 'translate_comments', 'prepare_comments'}
_COMMENT_PHASES = ('评论采集：', '评论评分：', '评论翻译：', '评论确认：')


def _json_column(column):
    """JSON1 仅提取所需字段，坏旧记录仍能返回目录与未知状态。"""
    return case((func.json_valid(column) == 1, column), else_='{}')


def _object(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _array(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, list) else None


def _chain(params):
    values = params.get('_chain')
    if not isinstance(values, list):
        return None
    remaining = [value for value in values if isinstance(value, str) and value in STEP_MAP]
    hold = params.get('_chain_hold')
    return dict(remaining=remaining, state='held' if params.get('_chain_state') == 'held' else 'active',
                hold={key: hold[key] for key in ('step', 'code', 'message') if key in hold}
                if isinstance(hold, dict) else None)


def _workflows(uids):
    with crud.session() as session:
        rows = session.exec(select(WorkflowRun.id, WorkflowRun.project_uid, WorkflowRun.status,
            WorkflowRun.phase, WorkflowRun.cursor, WorkflowRun.publication_id).where(
                WorkflowRun.project_uid.in_(uids)).order_by(WorkflowRun.created_at.desc(),
                                                           WorkflowRun.id.desc())).all()
    result = {}
    for row in rows:
        result.setdefault(row.project_uid, dict(id=row.id, status=row.status, phase=row.phase,
            cursor=row.cursor, publication_id=row.publication_id))
    return result


def _publications(uids):
    """白名单列与 JSON 子字段；不装载冻结文件方案、上传表或整份回执。"""
    from dubforge.execution_progress import normalize
    from dubforge.publishing import updates
    result = {uid: dict(id=None, status='pending', progress=0) for uid in uids}
    with crud.session() as session:
        transfer, receipt = _json_column(Publication.transfers), _json_column(Publication.receipt)
        rows = session.exec(select(Publication.id, Publication.project_uid, Publication.status,
            Publication.progress, Publication.error, Publication.revision, Publication.archived_at,
            Publication.updated_at, func.json_extract(transfer, '$._progress.execution').label('execution'),
            func.json_extract(receipt, '$.bvid').label('bvid'),
            func.json_extract(_json_column(Publication.chapter_sync), '$.status').label('chapter_status'))
            .where(Publication.project_uid.in_(uids), Publication.deleted_at.is_(None))
            .order_by(Publication.updated_at.desc(), Publication.id.desc())).all()
        latest, active = {}, {}
        for row in rows:
            latest.setdefault(row.project_uid, row)
            if row.archived_at is None:
                active.setdefault(row.project_uid, row)
        selected = {uid: active.get(uid) or latest.get(uid) for uid in uids}
        ids = [row.id for row in selected.values() if row is not None]
        update_execution = _json_column(PublicationUpdate.execution)
        # 更新执行记录还含整份传输/章节证据；列表只需要六个阶段的状态。
        execution = func.json_object('phase', func.json_extract(update_execution, '$.phase'),
            'detail', func.json_extract(update_execution, '$.detail'),
            'stages', func.json_extract(update_execution, '$.stages')).label('execution')
        update_rows = session.exec(select(PublicationUpdate.id, PublicationUpdate.source_publication_id,
            PublicationUpdate.status, PublicationUpdate.phase, PublicationUpdate.progress,
            PublicationUpdate.error, PublicationUpdate.error_code, PublicationUpdate.updated_at,
            execution).where(PublicationUpdate.source_publication_id.in_(ids))
            .order_by(PublicationUpdate.updated_at.desc(), PublicationUpdate.id.desc())).all() if ids else []
    latest_updates = {}
    for row in update_rows:
        latest_updates.setdefault(row.source_publication_id, row)
    for uid, row in selected.items():
        if row is None:
            continue
        result[uid] = dict(id=row.id, status=row.status, progress=row.progress, error=row.error,
            revision=row.revision, lifecycle='archived' if row.archived_at else 'active',
            updated_at=archived_at_iso(row.updated_at), bvid=row.bvid or '',
            execution=normalize(_object(row.execution), owner='publication', run_id=row.id)
                if row.status in _PUB_ACTIVE else None,
            chapter_sync=dict(status=row.chapter_status) if row.chapter_status else {})
        if row.id in latest_updates:
            result[uid]['latest_update'] = updates.progress_summary(latest_updates[row.id])
    return result


def _runtime():
    """一次复制正面占用事实，不能把读取失败或历史 failed 解释为执行中。"""
    from dubforge import video_info
    from dubforge.pipeline import dubbing_session
    from dubforge.runner import runner
    with runner._lock:
        running = dict(runner._current_jobs)
    with video_info.gate:
        info = dict(video_info._active)
    with dubbing_session.gate:
        dubbing = set(dubbing_session._active)
    return running, info, dubbing


def _revision(row, params, steps, jobs, workflow, publication, settings, batch, project_batch, busy):
    # 不含逐句进度/心跳/上传字节，防止完整核对在返回前永远被轻量轮询作废。
    update = publication.get('latest_update') or {}
    metadata = dict(project=[row.id, archived_at_iso(row.updated_at), row.status, row.title,
        row.source_url, row.source_kind, bool(row.archived_at)], params=params,
        steps=[[step.step_key, step.status, step.error_code, step.error_message,
                jobs.get((row.id, step.step_key)).id if (row.id, step.step_key) in jobs else None,
                jobs.get((row.id, step.step_key)).status if (row.id, step.step_key) in jobs else None]
               for step in steps], workflow={key: workflow.get(key) for key in
                   ('id', 'status', 'cursor', 'publication_id')} if workflow else None,
        publication=[publication.get('id'), publication.get('status'), publication.get('revision'),
                     update.get('id'), update.get('status'), update.get('error_code')],
        settings=settings,
        batch={key: batch.get(key) for key in ('id', 'status', 'position', 'item_status', 'feedback')}
            if batch else None,
        project_batch={key: project_batch.get(key) for key in
            ('batch_id', 'group_id', 'status', 'group_status', 'position', 'item_status', 'feedback')}
            if project_batch else None, busy=busy)
    return hashlib.sha256(json.dumps(metadata, sort_keys=True, ensure_ascii=False,
                                    default=str).encode('utf-8')).hexdigest()


def project_list(*, archive='active'):
    """新鲜度为未知；有确切活动事实才显示制作中。真实运行前仍需完整预检。"""
    if archive not in {'all', 'active', 'archived'}:
        raise ProjectArchiveError('项目列表范围无效，请选择活动项目、已归档或全部项目',
                                  code='PROJECT_ARCHIVE_FILTER_INVALID')
    raw = _json_column(Project.params)
    query = select(Project.id, Project.uid, Project.title, Project.title_mode, Project.source_url,
        Project.source_kind, Project.status, Project.created_at, Project.updated_at, Project.archived_at,
        func.json_extract(raw, '$.auto_steps').label('auto_steps'),
        func.json_extract(raw, '$._chain').label('chain'),
        func.json_extract(raw, '$._chain_state').label('chain_state'),
        func.json_extract(raw, '$._chain_hold').label('chain_hold'))
    if archive == 'active':
        query = query.where(Project.archived_at.is_(None))
    elif archive == 'archived':
        query = query.where(Project.archived_at.is_not(None))
    with crud.session() as session:
        projects = session.exec(query.order_by(Project.created_at.desc())).all()
        ids = [row.id for row in projects]
        steps = session.exec(select(StepRun.project_id, StepRun.step_key, StepRun.status,
            StepRun.progress, StepRun.error_code, StepRun.error_message).where(
                StepRun.project_id.in_(ids))).all() if ids else []
    if not projects:
        return []
    from dubforge import project_batches, release_batches
    from dubforge.project_settings_summary import project_summaries
    from dubforge.runner import runner
    uids = [row.uid for row in projects]
    jobs = runner.latest_execution_jobs(ids)
    workflows = _workflows(uids)
    publications = _publications(uids)
    batches, project_groups = release_batches.project_statuses(uids), project_batches.project_statuses(uids)
    settings = project_summaries([SimpleNamespace(uid=uid) for uid in uids], include_display_names=False)
    running, info, dubbing = _runtime()
    grouped = {}
    for step in steps:
        if step.step_key in PIPELINE_KEYS:
            grouped.setdefault(step.project_id, []).append(step)
    result = []
    for project in projects:
        uid = project.uid
        saved = {step.step_key: step for step in grouped.get(project.id, [])}
        auto = _array(project.auto_steps)
        params = dict(auto_steps=[key for key in auto if isinstance(key, str) and key in STEP_MAP]) if auto is not None else {}
        chain = _array(project.chain)
        if chain is not None:
            hold = _object(project.chain_hold)
            params.update(_chain=[key for key in chain if isinstance(key, str) and key in STEP_MAP],
                _chain_state=project.chain_state,
                _chain_hold={key: hold[key] for key in ('step', 'code', 'message') if key in hold})
        selected = set(params.get('auto_steps', PIPELINE_KEYS))
        views, runtime_steps = [], []
        for key in PIPELINE_KEYS:
            step = saved.get(key)
            job = jobs.get((project.id, key))
            status = step.status if step else 'pending'
            # 同步骤最新 Job 是执行事实，旧 StepRun 的失败提示不能盖住已受理的新任务。
            if job and job.status in _STEP_ACTIVE and (status not in _STEP_ACTIVE or job.status == 'queued'):
                status = job.status
            elif job and status in _STEP_ACTIVE and job.status in {'done', 'failed', 'cancelled', 'dead'}:
                # 步骤与任务分两次批读；后读的终态不能被前读的旧 running 再投影成活动。
                status = {'cancelled': 'interrupted', 'dead': 'interrupted'}.get(job.status, job.status)
            active = status in _STEP_ACTIVE
            if active:
                runtime_steps.append(SimpleNamespace(step_key=key, status=status,
                                                      error_message=step.error_message if step else ''))
            views.append(dict(key=key, label=STEP_MAP[key].label, status=status,
                recorded_status=step.status if step else 'pending', selected=key in selected,
                progress=round(step.progress or 0, 1) if step and step.status == status else 0,
                fresh=None, freshness_known=False, error=step.error_message if step and status == 'failed' else None,
                job_id=job.id if job else None, job_status=job.status if job else None, execution=None))
        executions = runner.execution_details(project.id, runtime_steps, jobs=jobs)
        for value in views:
            if value['key'] in executions:
                if value['status'] == 'running':
                    value.update(runner.progress_eta(project.id, value['key']))
                value['execution'] = executions[value['key']]
        workflow = workflows.get(uid)
        publication = publications[uid]
        batch, group = batches.get(uid), project_groups.get(uid)
        current = next((value['key'] for value in views if value['status'] in _STEP_ACTIVE), None)
        comment_busy = info.get(uid) in _COMMENT_ACTIONS or bool(workflow and workflow['status'] == 'running'
            and isinstance(workflow['phase'], str) and workflow['phase'].startswith(_COMMENT_PHASES))
        memory_busy = any(pid == project.id for pid, _key in running) or uid in info or uid in dubbing
        busy = bool(current or memory_busy or (workflow and workflow['status'] in _FLOW_ACTIVE)
            or publication['status'] in _PUB_ACTIVE
            or (publication.get('latest_update') or {}).get('status') in _UPDATE_ACTIVE
            or (publication.get('latest_update') or {}).get('busy')
            or (batch and batch['status'] in release_batches.ACTIVE)
            or (group and group['status'] in project_batches.ACTIVE))
        comments = dict(state='running', label='评论处理中',
            detail='后台正在处理评论，详细数量在评论工作区查看') if comment_busy else None
        result.append(dict(uid=uid, title=project.title, title_mode=project.title_mode,
            source_url=project.source_url, source_kind=project.source_kind,
            status='processing' if busy else 'checking', recorded_status=project.status,
            projection='summary', freshness_known=False,
            summary_revision=_revision(project, params, list(saved.values()), jobs, workflow,
                publication, settings[uid], batch, group, [memory_busy, comment_busy]),
            archived=project.archived_at is not None, archived_at=archived_at_iso(project.archived_at),
            dir_exists=None, created_at=archived_at_iso(project.created_at), params=params, chain=_chain(params),
            running_step=current, running_label=STEP_MAP[current].label if current else
                ('评论处理中' if comment_busy else '后台执行中' if busy else None),
            workflow_status=workflow['status'] if workflow else None, workflow_summary=workflow,
            publication=publication, release_batch=batch, project_batch=group,
            project_settings=settings[uid], comments_workspace=comments, steps=views))
    return result
