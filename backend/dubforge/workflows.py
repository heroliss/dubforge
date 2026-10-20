"""全流程协调器：制作、评论和发布仍各自负责产物与副作用，本模块只衔接已确认范围。"""
import copy
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from sqlmodel import Session, select

from dubforge import comment_studio, video_info
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.recovery import RecoveryDependencies as Dependencies
from dubforge.pipeline.assembly_outputs import final_variant, dimensions
from dubforge.pipeline.registry import STEP_MAP
from dubforge.publishing import service
from dubforge.workflow_models import WorkflowRun
from dubforge.workflow_plan import LABELS, WorkflowError, automatic_copy_fields, configuration_stamp, digest, plan, recipe

_lock = threading.RLock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='workflow')
_running = {}
_leases = {}
_owner = threading.local()
_monitor_stop = threading.Event()
_monitor = None
BUSY = {'queued', 'running', 'stopping'}
OPEN = BUSY | {'monitoring'}


def _is_update(run):
    return (run.get('recipe') or {}).get('mode') == 'update'


def _update_task(run):
    from dubforge import workflow_update
    return workflow_update.get_task(run)


def _update_execution(task, previous=None):
    """更新任务负责业务状态；这里只转换为统一的过程展示。"""
    from dubforge.execution_progress import Recorder
    execution = task.get('execution') or {}
    detail = (execution.get('delivery') or {}).get('execution')
    state = ('completed' if task['status'] == 'completed' else 'attention' if task['status'] in {'failed', 'unknown'}
             else 'paused' if task['status'] == 'paused' else 'waiting' if task['status'] == 'waiting' else 'running')
    progress = Recorder(previous=detail or previous, owner='publication_update', run_id=task['id'])
    return progress.phase('publish.update.' + task['status'], task.get('phase') or '更新原稿件', state=state,
        detail=task.get('error', ''), next_check_at=task.get('next_check') or None,
        metrics={k: execution[k] for k in ('current_part', 'loaded_bytes', 'total_bytes') if k in execution})


def _sync_update_task(key, task):
    ready = task['status'] == 'uploaded' or (task['status'] == 'prepared' and task.get('delivery_mode') == 'draft')
    status = ('ready' if ready else 'completed' if task['status'] == 'completed' else 'attention' if task['status'] in {'failed', 'unknown', 'prepared'}
              else 'paused' if task['status'] == 'paused' else 'monitoring')
    _update(key, status=status, phase=task.get('phase') or '更新原稿件', error=task.get('error', ''),
            next_check=time.time() + 3 if status == 'monitoring' else 0, check_failures=0,
            execution=_update_execution(task, get(key).get('execution')))
    return status


def _update_recovery(run, task):
    recovery = dict(execute_steps=[], start_step='',
        detail='继续原稿更新的已确认范围，复用已完成的制作、上传及同步结果。')
    fingerprint = digest(dict(fingerprint=run['plan']['fingerprint'], update_id=task['id'],
                              status=task['status'], update_fingerprint=task['fingerprint'], recovery=recovery))
    return recovery, fingerprint


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def check_idle(uid):
    # 不反向获取启动/编辑锁；调用方在自己的事务锁内核对租约。
    from dubforge import release_batches
    release_batches.check_idle(uid)
    from dubforge import project_batches
    project_batches.check_idle(uid)
    lease = _leases.get(uid)
    if lease and getattr(_owner, 'key', None) != lease:
        raise WorkflowError('本项目全流程正在运行，请先停止全流程再手动运行或修改制作资料', code='WORKFLOW_BUSY')


@contextmanager
def _owned(key):
    previous = getattr(_owner, 'key', None)
    _owner.key = key
    try:
        yield
    finally:
        _owner.key = previous


def _row(session, key):
    row = session.get(WorkflowRun, key)
    if row is None:
        raise WorkflowError('全流程任务不存在', code='WORKFLOW_NOT_FOUND')
    return row


def _view(row):
    data = row.model_dump(mode='json')
    for field in ('recipe', 'plan', 'execution'):
        data[field] = json.loads(data[field])
    if row.publication_id and not _is_update(data):
        try:
            pub = service.view(row.publication_id)
            data['publication'] = {k: pub[k] for k in ('id', 'status', 'progress', 'error', 'receipt')}
            data['publication']['chapter_sync'] = pub.get('chapter_sync') or {}
            if row.status == 'monitoring':
                chapter = pub.get('chapter_sync') or {}
                options = pub.get('snapshot', {}).get('chapter_options') or {}
                data['execution'] = (chapter.get('execution') if options.get('enabled') and options.get('items')
                    and pub['status'] in {'published', 'private', 'reviewed'} else pub.get('execution')) or data['execution']
        except service.PublishError:
            pass
    if _is_update(data):
        data['mode'] = 'update'
        data['update_id'] = data['plan'].get('publication_update_id', '')
        task = _update_task(data)
        if task:
            data['update_id'] = task['id']
            data['publication_update'] = task
            data['publication'] = {**task, 'receipt': {'bvid': task.get('bvid', '')}}
            if row.status == 'monitoring':
                data['execution'] = _update_execution(task, data.get('execution'))
    from dubforge.execution_progress import normalize
    data['execution'] = normalize(data.get('execution'), state='stopping' if row.status == 'stopping' else 'paused' if row.post_processing_paused
        else 'completed' if row.status == 'ready' else row.status, owner='workflow', run_id=row.id)
    if row.status == 'monitoring' and row.next_check and not data['execution'].get('next_check_at'):
        data['execution']['next_check_at'] = row.next_check
    data['phase'] = _legacy_stopped_phase(data)
    return data


def get(key):
    with Session(get_engine()) as session:
        return _view(_row(session, key))


def _same_request(row, uid, body):
    if (row.project_uid != uid or row.recipe != _json(recipe(body.get('recipe')))
            or json.loads(row.plan)['fingerprint'] != body.get('fingerprint')):
        raise WorkflowError('同一启动标识不能提交不同项目或方案', code='WORKFLOW_CONFLICT')
    return _view(row)


def _update(key, **fields):
    with _lock, Session(get_engine()) as session:
        row = _row(session, key)
        for name, value in fields.items():
            setattr(row, name, _json(value) if name in {'recipe', 'plan', 'execution'} else value)
        session.add(row)
        session.commit()


def _accepted_execution(key, previous=None, *, label='检查完成，等待执行',
                      phase='workflow.queued', state='queued'):
    """受理和新阶段一起落盘，排队期间不能继续展示上次失败正文。"""
    from dubforge.execution_progress import Recorder
    return Recorder(previous=previous, owner='workflow', run_id=key, restart=True).phase(
        phase, label, state=state)


def view(uid):
    if not crud.get_project(uid):
        raise WorkflowError('项目不存在')
    with Session(get_engine()) as session:
        from dubforge import configuration
        values = configuration.run_spec(uid)
        settings = configuration.resolve(uid)
        runs = session.exec(select(WorkflowRun).where(WorkflowRun.project_uid == uid)
                            .order_by(WorkflowRun.created_at.desc()).limit(10)).all()
        from dubforge.pipeline.assembly_versions import catalog
        version_catalog = catalog(uid)
        return dict(recipe=values, revision=settings['revision'],
                    has_remote_publication=_has_remote_publication(session, uid),
                    version_catalog=version_catalog,
                    origin=settings['parent'] or '全局默认', mode_source=settings['sources']['run.mode'],
                    variants=[dict(value=k, label=v) for k, v in LABELS.items()],
                    runs=[_view(row) for row in runs])


def status(uid):
    """看板的轻量只读状态：不扫描成品、不预检服务、不查询 B站。"""
    from dubforge import configuration
    from dubforge.publishing.models import Publication
    if not crud.get_project(uid):
        raise WorkflowError('项目不存在', code='PROJECT_NOT_FOUND')
    settings = configuration.resolve(uid)
    with Session(get_engine()) as session:
        rows = select(WorkflowRun).where(WorkflowRun.project_uid == uid)
        row = session.exec(rows.order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())).first()
        previous = session.exec(rows.where(WorkflowRun.publication_id != '')
                                .order_by(WorkflowRun.created_at.desc())).first()
        run = None
        if row:
            spec, plan = json.loads(row.recipe), json.loads(row.plan)
            steps = plan.get('steps', [])
            publication_receipt = None
            run = dict(id=row.id, status=row.status, mode=spec.get('mode'), phase=row.phase,
                       delivery_mode=spec.get('delivery_mode', 'publish' if spec.get('mode') == 'update' else spec.get('mode')),
                       cursor=row.cursor, total=len(steps), error=row.error, publication_id=row.publication_id,
                       execution=json.loads(row.execution), post_processing_paused=row.post_processing_paused)
            if row.publication_id and spec.get('mode') != 'update' and (pub := session.get(Publication, row.publication_id)):
                run['publication'] = dict(id=pub.id, status=pub.status, progress=pub.progress, error=pub.error,
                                          chapter_sync=json.loads(pub.chapter_sync))
                if row.status == 'paused' and row.phase == _LEGACY_STOPPING_PHASE:
                    publication_receipt = json.loads(pub.receipt)
                if row.status == 'monitoring':
                    from dubforge.publishing import chapter_sync
                    chapter = chapter_sync.state_view(pub)
                    options = json.loads(pub.snapshot).get('chapter_options') or {}
                    run['execution'] = (chapter.get('execution') if options.get('enabled') and options.get('items')
                        and pub.status in {'published', 'private', 'reviewed'} else service._execution_view(pub))
            if spec.get('mode') == 'update':
                task = _update_task(dict(id=row.id, recipe=spec, plan=plan))
                run['update_id'] = plan.get('publication_update_id', '')
                if task:
                    run['update_id'] = task['id']
                    run['publication_update'] = task
                    run['publication'] = {**task, 'receipt': {'bvid': task.get('bvid', '')}}
                    if row.status == 'monitoring':
                        run['execution'] = _update_execution(task, run['execution'])
            from dubforge.execution_progress import normalize
            run['execution'] = normalize(run['execution'], state='stopping' if row.status == 'stopping' else 'paused' if row.post_processing_paused
                else 'completed' if row.status == 'ready' else row.status, owner='workflow', run_id=row.id)
            if row.status == 'monitoring' and row.next_check and not run['execution'].get('next_check_at'):
                run['execution']['next_check_at'] = row.next_check
            run['phase'] = _legacy_stopped_phase(run, receipt=publication_receipt)
        values = settings['values']
        from dubforge.project_execution import production_selection
        selected = production_selection(values)
        target = values.get('publish.project_target') or {}
        metadata_only = (values['run.mode'] != 'produce' and target.get('kind') == 'existing'
                         and target.get('replace_videos') is False and 'add_parts' not in target.get('sync_fields', []))
        if metadata_only:
            selected = dict(version_ids=[], source='not_needed', blockers=[])
        definitions = {item['id']: item for item in values['assembly.versions']}
        target_publication = session.get(Publication, target.get('publication_id', '')) if target.get('kind') == 'existing' else None
        try:
            receipt = json.loads(target_publication.receipt) if target_publication and target_publication.project_uid == uid else {}
        except (TypeError, ValueError):
            receipt = {}
        if not isinstance(receipt, dict):
            receipt = {}
        project_goal = dict(production_version_ids=selected['version_ids'],
            target_versions=[dict(id=key, name=definitions[key]['name']) for key in selected['version_ids'] if key in definitions],
            target_source=selected['source'], target_blockers=selected['blockers'],
            production_required=not metadata_only,
            delivery_mode=values['run.mode'], target_kind=target.get('kind', ''),
            target_selection=copy.deepcopy(settings.get('target_selection')),
            target_publication_id=target.get('publication_id', ''), target_bvid=receipt.get('bvid', ''),
            replace_videos=target.get('replace_videos', False), sync_fields=target.get('sync_fields', []))
        return dict(mode=values['run.mode'], mode_source=settings['sources']['run.mode'], run=run,
                    project_goal=project_goal,
                    has_remote_publication=_has_remote_publication(session, uid),
                    existing_publication_id=previous.publication_id if previous else '')


def _has_remote_publication(session, uid):
    """手动发布也属于原稿件；本地草稿不构成已有BV的证据。"""
    from dubforge.publishing.models import Publication
    rows = session.exec(select(Publication).where(Publication.project_uid == uid,
        Publication.status.in_(service.REMOTE_COPY_STATUSES))).all()
    for row in rows:
        try:
            receipt = json.loads(row.receipt)
        except (TypeError, ValueError):
            continue
        if isinstance(receipt, dict) and isinstance(receipt.get('bvid'), str) and receipt['bvid']:
            return True
    return False


def _new_publication_issues(session, uid, spec):
    """默认首次发布在真正入队前再核对历史；显式新稿也不能绕过未知回执。"""
    if spec.get('mode') in {'produce', 'update'}:
        return []
    from dubforge.publishing.models import Publication
    if session.exec(select(Publication.id).where(Publication.project_uid == uid,
                                               Publication.status == 'unknown')).first():
        return ['投稿回执不明，请先到发布页核对已有稿件；不会创建重复稿件或自动重投']
    if spec.get('confirm_new_publication'):
        return []
    if spec.get('target_driven'):
        from dubforge.project_publish_target import history_projects
        if uid in history_projects(session, [uid]):
            return ['项目已有发布记录，请明确选择创建新稿或更新原稿后重新检查']
    elif session.exec(select(WorkflowRun.id).where(WorkflowRun.project_uid == uid,
                                                 WorkflowRun.publication_id != '')).first():
        return ['已有自动流程发布记录，请选择更新原稿件；若要新建另一个BV，请明确确认新建，不会创建重复稿件']
    elif _has_remote_publication(session, uid):
        return ['项目已有B站稿件，请选择更新原稿件；若要新建另一个BV，请明确确认新建，不会创建重复稿件']
    return []


def _availability(uid, mode, resume_id=None, *, confirm_new_publication=False, target_driven=False):
    """预检提前解释执行阻塞；启动锁内仍保留最终校验，不能以页面状态代替。"""
    issues = []
    if resume_id is not None and (not isinstance(resume_id, str) or not resume_id or len(resume_id) > 100):
        raise WorkflowError('继续任务标识无效，请重新打开运行检查')
    project = crud.get_project(uid)
    if project and project.archived_at is not None:
        issues.append('项目已归档，请先恢复项目后再运行')
    with Session(get_engine()) as session:
        rows = select(WorkflowRun).where(WorkflowRun.project_uid == uid)
        if resume_id:
            row = session.get(WorkflowRun, resume_id)
            if not row or row.project_uid != uid:
                raise WorkflowError('任务不属于当前项目')
            if row.status not in {'paused', 'failed', 'attention'}:
                issues.append('当前流程不能继续，请刷新流程状态')
            if row.publication_id and mode != 'update':
                issues.append('制作已完成，请到发布页继续现有稿件，不会重新制作或重复投稿')
        if session.exec(rows.where(WorkflowRun.status.in_(OPEN))).first():
            issues.append('已有自动流程正在执行或等待审核，请查看当前进度，不要重复启动')
        if not resume_id:
            issues.extend(_new_publication_issues(session, uid, dict(mode=mode,
                confirm_new_publication=confirm_new_publication, target_driven=target_driven)))
    if project and any(s.status in {'queued', 'running'} for s in crud.get_steps(project.id)):
        issues.append('制作步骤正在执行或收尾，请等待完成后再运行完整流程')
    return issues


def prepared_plan(uid, spec, *, online=True, dubbing_operation_id=None, previous_update=None):
    """批次以已确认的配方预检，不改项目配置或继承来源。"""
    result = plan(uid, spec, online=online, dubbing_operation_id=dubbing_operation_id,
                  previous_update=previous_update)
    return _with_comments(result)


def _with_comments(result):
    spec = result['recipe']
    comments = any(v['include_comments'] and not v.get('update') and not v.get('timeline') for v in result['version_specs']) if 'version_specs' in result else any(dimensions(v)['include_comments'] for v in spec['variants'])
    if comments and spec['comments_mode'] == 'auto' and 'assemble' in result.get('steps', []):
        result['steps'].insert(result['steps'].index('assemble'), 'comments')
        result['labels']['comments'] = '采集、精选并翻译上屏评论'
        if 'comments' not in result.get('requested_steps', []) and 'comments' not in result.get('added_steps', []):
            added = result.setdefault('added_steps', [])
            insert_at = added.index('assemble') if 'assemble' in added else len(added)
            added.insert(insert_at, 'comments')
    return result


def _plan(uid, raw, *, online=True, previous_update=None):
    from dubforge import configuration
    intent = raw or {}
    previous_run = get(intent['resume_id']) if intent.get('resume_id') else None
    requested_mode = intent.get('mode')
    if intent.get('target_driven') is True and not previous_run:
        requested_mode = None
    if requested_mode is None and previous_run and _is_update(previous_run):
        requested_mode = 'update'
    # 恢复只延续上次确认的范围与远程授权；当前项目改成投稿不能升级旧任务。
    if previous_run and previous_run.get('recipe', {}).get('mode'):
        spec = copy.deepcopy(previous_run['recipe'])
    elif previous_run:
        # 早期不完整记录没有可证明的投稿授权，最多恢复为仅制作。
        spec = configuration.run_spec(uid, mode='produce')
    else:
        spec = configuration.run_spec(uid, mode=requested_mode,
            **({'target_driven': True} if intent.get('target_driven') is True else {}))
    if not previous_run and spec.get('target_driven') and 'from_step' in intent:
        spec['from_step'] = intent['from_step']
    if 'confirm_new_publication' in intent and not previous_run:
        spec['confirm_new_publication'] = intent['confirm_new_publication']
    elif previous_run and 'confirm_new_publication' in previous_run.get('recipe', {}):
        spec['confirm_new_publication'] = previous_run['recipe']['confirm_new_publication']
    if spec['mode'] == 'update':
        target = spec.get('update') if previous_run or spec.get('target_driven') else intent.get('update')
        if intent.get('resume_id'):
            if target is None:
                target = previous_run.get('recipe', {}).get('update')
            previous_update = previous_update or previous_run.get('plan', {}).get('update_plan')
        spec['update'] = copy.deepcopy(target)
        if (isinstance(target, dict) and target.get('replace_videos', True) and target.get('replacements')
                and 'add_parts' not in target.get('sync_fields', [])):
            ids = [item.get('version_id') for item in target['replacements'] if isinstance(item, dict)]
            spec['version_ids'] = ids
            spec['part_titles'] = {key: title for key, title in spec['part_titles'].items() if key in ids}
    # The checkbox scope is a run intent, not project auto_steps.  Resume
    # requests from older clients may omit it; in that case preserve the
    # frozen scope stored with the interrupted run instead of expanding back
    # to the legacy full recipe.
    if 'production_steps' in intent and not previous_run and not spec.get('target_driven'):
        spec['production_steps'] = intent['production_steps']
    elif intent.get('resume_id'):
        try:
            previous = get(intent['resume_id'])
            stored_recipe = previous.get('recipe') or {}
            previous_recipe = (json.loads(stored_recipe) if isinstance(stored_recipe, str)
                               else stored_recipe)
            if 'production_steps' in previous_recipe:
                spec['production_steps'] = previous_recipe['production_steps']
        except (DubForgeError, TypeError, ValueError, json.JSONDecodeError):
            pass
    if 'dubbing' in intent and not previous_run:
        spec['dubbing'] = intent['dubbing']
    elif intent.get('resume_id'):
        previous = get(intent['resume_id'])
        if previous and 'dubbing' in (previous.get('recipe') or {}):
            spec['dubbing'] = previous['recipe']['dubbing']
    return _with_comments(plan(uid, spec, online=online, dubbing_operation_id=intent.get('resume_id'),
                               previous_update=previous_update))


def start_prepared(uid, candidate, request_id, *, existing_id='', publication_id='', frozen_target=False):
    """仅已获得批次租约的内部协调器可执行冻结配方；仍通过全部公共启动锁。"""
    from dubforge import release_batches
    from dubforge.project_archive import assert_active
    from dubforge.runner import runner
    from dubforge.pipeline import dubbing_session
    updating = candidate.get('recipe', {}).get('mode') == 'update'
    if updating and not candidate['recipe'].get('target_driven'):
        raise WorkflowError('顺序发布批次暂不接管原稿更新，请从单项目运行检查中确认更新范围')
    from dubforge import project_batches
    batch_owner = release_batches if release_batches.owns(uid) else project_batches
    if not batch_owner.owns(uid):
        raise WorkflowError('批次执行身份无效', code='WORKFLOW_BATCH_OWNER')
    with service._lock, runner._start_lock, dubbing_session.gate, video_info.gate, _lock:
        check_idle(uid)
        service.check_project_idle(uid)
        video_info.check_idle(uid)
        dubbing_session.check_idle(uid)
        project = assert_active(uid)
        if any(s.status in {'running', 'queued'} for s in crud.get_steps(project.id)):
            raise WorkflowError('项目已有制作任务，请等待完成')
        refresh_publication = bool(candidate.get('refresh_publication'))
        update_task = _update_task(get(existing_id)) if updating and existing_id else None
        if refresh_publication and candidate['refresh_publication'] != publication_id:
            raise WorkflowError('待更新草稿与确认方案不一致', code='WORKFLOW_PLAN_CHANGED')
        if not publication_id or refresh_publication or updating:
            if update_task:
                previous_run = get(existing_id)
                _recovery, recovery_fingerprint = _update_recovery(previous_run, update_task)
                if candidate.get('recovery_fingerprint') != recovery_fingerprint:
                    raise WorkflowError('原稿交付状态在批次确认后变化，请重新检查', code='WORKFLOW_PLAN_CHANGED')
                current = candidate
            elif candidate['recipe'].get('target_driven') and not existing_id and not frozen_target:
                from dubforge import configuration
                current = prepared_plan(uid, configuration.run_spec(uid, target_driven=True), online=False,
                                        previous_update=candidate.get('update_plan'))
            else:
                current = prepared_plan(uid, candidate['recipe'], online=False,
                    dubbing_operation_id=existing_id or None, previous_update=candidate.get('update_plan'))
            if current['fingerprint'] != candidate['fingerprint']:
                raise WorkflowError('批次预检后设置已变化，请重新确认', code='WORKFLOW_PLAN_CHANGED')
            if current['blockers']:
                raise WorkflowError('；'.join(current['blockers']), code='WORKFLOW_PREFLIGHT')
        with batch_owner.launching(uid), Session(get_engine()) as session:
            active = session.exec(select(WorkflowRun).where(WorkflowRun.project_uid == uid, WorkflowRun.status.in_(BUSY))).first()
            if active:
                if active.request_id == request_id:
                    return _view(active)
                raise WorkflowError('项目已有完整流程正在运行')
            row = _row(session, existing_id) if existing_id else session.exec(select(WorkflowRun).where(WorkflowRun.request_id == request_id)).first()
            if row and row.project_uid != uid:
                raise WorkflowError('保留流程不属于本项目')
            if row is None:
                if not publication_id:
                    issues = _new_publication_issues(session, uid, candidate['recipe'])
                    if issues:
                        raise WorkflowError('；'.join(issues), code='WORKFLOW_PLAN_CHANGED')
                row = WorkflowRun(project_uid=uid, request_id=request_id)
            elif row.publication_id and publication_id != row.publication_id:
                raise WorkflowError('批次不能替换已有关联稿件', code='WORKFLOW_CONFLICT')
            cursor = 0
            current = copy.deepcopy(candidate)
            if row.plan != '{}' and not update_task and (not publication_id or refresh_publication or updating):
                current, cursor = _recovery_plan(_view(row), current, project)
            if publication_id:
                pub = service.view(publication_id)
                if pub['project_uid'] != uid or pub['status'] == 'unknown':
                    raise WorkflowError('投稿结果待核对，请继续原稿件，不会自动重投')
                if refresh_publication and (pub['status'] != 'draft' or pub['receipt'].get('bvid')):
                    raise WorkflowError('草稿已上传或投稿，请重新确认继续原记录；不会替换已有稿件', code='WORKFLOW_PLAN_CHANGED')
                if refresh_publication:
                    binding = current.get('publication_binding') or {}
                    if (pub.get('archived_at') or pub.get('deleted_at') or pub['revision'] != binding.get('revision')
                            or digest(pub['draft']) != binding.get('fingerprint')):
                        raise WorkflowError('确认后的草稿已变化、归档或移入回收站，请重新检查；未启动制作', code='WORKFLOW_PLAN_CHANGED')
                row.publication_id = publication_id
                if not refresh_publication and not updating:
                    cursor = len(current['steps'])
            # 批次重新接管同一流程时，公开预检方案没有内部恢复回执；
            # 不能因刷新计划而让已受理的旧 UUID 获得第二次执行机会。
            receipts = json.loads(row.plan).get('_resume_requests')
            if isinstance(receipts, dict) and receipts:
                proposed = current.get('_resume_requests')
                current['_resume_requests'] = {**(copy.deepcopy(proposed) if isinstance(proposed, dict) else {}),
                                               **copy.deepcopy(receipts)}
            row.recipe, row.plan = _json(current['recipe']), _json(current)
            row.cursor, row.status, row.error = cursor, 'queued', ''
            row.phase = '恢复检查完成，等待执行' if existing_id else '检查完成，等待执行'
            row.execution = _json(_accepted_execution(row.id, json.loads(row.execution), label=row.phase))
            row.post_processing_paused = False
            session.add(row)
            session.commit()
            session.refresh(row)
            key = row.id
            runner._clear_chain(project, '切换到已确认的顺序发布批次')
            batch_owner.bind_workflow(uid, key)
            _launch(key, uid)
    return get(key)


def _plan_dependencies(project, candidate):
    """恢复说明与执行使用相同的成片版本、配音及字幕依赖。"""
    deps = Dependencies(project, assemble_variants=[v for v in candidate['recipe']['variants'] if v != 'src'] or None)
    return deps.with_versions(candidate['version_specs']) if 'version_specs' in candidate else deps


def _recovery_plan(run, candidate, project=None):
    """只读解析真正的恢复起点；预检和入队前共用，不能各自修复游标。"""
    from dubforge.pipeline.recovery import cache_usable, describe
    candidate = copy.deepcopy(candidate)
    previous = run.get('plan') or {}
    previous_steps = previous.get('steps') or []
    previous_cursor = run.get('cursor', 0)
    changed = candidate['fingerprint'] != previous.get('fingerprint')
    cursor = 0 if changed else previous_cursor
    project = project or crud.get_project(run['project_uid'])
    deps = _plan_dependencies(project, candidate)
    if not changed:
        # 已有产物会缩短新计划；原游标仍须对应最初确认的步骤表。
        if set(candidate['steps']) - set(previous_steps):
            cursor = 0  # 新增缺失依赖按新计划补齐，不能套用原游标。
        else:
            candidate['steps'] = copy.deepcopy(previous_steps)
            candidate['labels'] = {**candidate['labels'], **previous.get('labels', {})}
            for index, step in enumerate(candidate['steps'][:cursor]):
                if step != 'comments' and not cache_usable(deps, step):
                    cursor = index
                    break
    candidate['recovery'] = describe(deps, candidate['steps'], cursor=cursor,
        previous_steps=previous_steps, previous_cursor=previous_cursor, changed=changed)
    from dubforge.pipeline import dubbing_policy
    dubbing_policy.apply_recovery(candidate.get('dubbing'), candidate['recovery'])
    # 句数合同损坏时，恢复说明中的上游链必须进入真正执行的步骤表。
    # 旧流程可能只登记了 tts/assemble；若只展示 execute_steps 而不更新
    # steps，执行器仍会跳过 translate，下一步又在 TTS 处重复报错。其他
    # 恢复场景继续沿用原步骤表与游标，避免把普通的历史缺口扩大成整链重跑。
    alignment_step = candidate['recovery'].get('alignment_step')
    recovery_steps = set(candidate['recovery'].get('execute_steps') or [])
    if alignment_step or recovery_steps:
        order = ['download', 'preprocess', 'transcribe', 'split', 'translate',
                 'dubtask', 'tts', 'subtitles', 'comments', 'assemble']
        existing = set(candidate['steps'])
        # 恢复描述已经计算出完整的待执行链。旧流程只保存了 tts/assemble
        # 时，不能只按 alignment_step 补一个 translate：如果 split 也因
        # 上游合同或输入指纹失效，跳过 split 会再次生成不匹配的译文。
        needed = recovery_steps | ({alignment_step} if alignment_step else set())
        candidate['steps'] = [key for key in order if key in existing or key in needed]
        candidate['labels'] = {**candidate.get('labels', {}), **{
            key: ('评论准备' if key == 'comments' else STEP_MAP[key].label)
            for key in candidate['steps'] if key in STEP_MAP or key == 'comments'}}
        start_step = candidate['recovery'].get('start_step')
        cursor = candidate['steps'].index(start_step) if start_step in candidate['steps'] else len(candidate['steps'])
    candidate['recovery_fingerprint'] = digest(dict(
        fingerprint=candidate['fingerprint'], recovery=candidate['recovery']))
    return candidate, cursor


def preview(uid, raw):
    resume_id = (raw or {}).get('resume_id')
    if resume_id:
        previous = get(resume_id)
        if previous['project_uid'] != uid:
            raise WorkflowError('任务不属于当前项目')
        if _is_update(previous) and _update_task(previous):
            task = _update_task(previous)
            result = copy.deepcopy(previous['plan'])
            result['recipe'] = previous['recipe']
            result['blockers'] = _availability(uid, 'update', resume_id)
            result['recovery'], result['recovery_fingerprint'] = _update_recovery(previous, task)
            result['publication_update'] = task
            return result
    result = _plan(uid, raw)
    blockers = _availability(uid, result['recipe']['mode'], resume_id,
                             confirm_new_publication=result['recipe'].get('confirm_new_publication', False),
                             target_driven=result['recipe'].get('target_driven', False))
    if resume_id:
        run = get(resume_id)
        if run['status'] in {'paused', 'failed', 'attention'} and (not run['publication_id'] or _is_update(run)):
            result, _cursor = _recovery_plan(run, result)
    else:
        from dubforge.pipeline.recovery import describe
        result['recovery'] = describe(_plan_dependencies(crud.get_project(uid), result), result['steps'])
    result['blockers'] = list(dict.fromkeys(result['blockers'] + blockers))
    return result


def start(uid, body):
    from dubforge.project_archive import assert_active
    if body.get('confirmed') is not True:
        raise WorkflowError('请确认本次流程的终点、分P顺序与发布范围')
    request = body.get('request_id')
    if not isinstance(request, str) or len(request) != 36:
        raise WorkflowError('请重新预检后启动')
    try:
        uuid.UUID(request)
    except ValueError as exc:
        raise WorkflowError('启动标识无效') from exc
    with Session(get_engine()) as session:
        existing = session.exec(select(WorkflowRun).where(WorkflowRun.request_id == request)).first()
        if existing:
            return _same_request(existing, uid, body)
    assert_active(uid)
    candidate = _plan(uid, body.get('recipe'))
    if candidate['blockers']:
        raise WorkflowError('；'.join(candidate['blockers']), code='WORKFLOW_PREFLIGHT')
    if body.get('fingerprint') != candidate['fingerprint']:
        raise WorkflowError('设置或评论方案已变化，请重新预检', code='WORKFLOW_CONFLICT')
    from dubforge.runner import runner
    from dubforge.pipeline import dubbing_session
    # 与手动运行、编辑及投稿使用同一锁顺序。网络预检在持锁前完成。
    with service._lock, runner._start_lock, dubbing_session.gate, video_info.gate, _lock:
        check_idle(uid)
        service.check_project_idle(uid)
        project = assert_active(uid)
        if any(s.status in {'queued', 'running'} for s in crud.get_steps(project.id)):
            raise WorkflowError('项目已有制作任务，请等待完成')
        video_info.check_idle(uid)
        dubbing_session.check_idle(uid)
        # 确认后到入队之间的本地修改仍必须拦截。
        if _plan(uid, candidate['recipe'], online=False,
                 previous_update=candidate.get('update_plan'))['fingerprint'] != candidate['fingerprint']:
            raise WorkflowError('预检期间设置已变化，请重新确认', code='WORKFLOW_CONFLICT')
        with Session(get_engine()) as session:
            active = session.exec(select(WorkflowRun).where(WorkflowRun.project_uid == uid, WorkflowRun.status.in_(OPEN))).first()
            if active:
                if active.request_id == request:
                    return _same_request(active, uid, body)
                raise WorkflowError('已有全流程在运行或等待平台审核，请查看已有记录')
            previous = session.exec(select(WorkflowRun).where(WorkflowRun.request_id == request)).first()
            if previous:
                return _same_request(previous, uid, body)
            issues = _new_publication_issues(session, uid, candidate['recipe'])
            if issues:
                raise WorkflowError('；'.join(issues), code='WORKFLOW_CONFLICT')
            row = WorkflowRun(project_uid=uid, request_id=request, recipe=_json(candidate['recipe']), plan=_json(candidate),
                publication_id=candidate['recipe']['update']['publication_id'] if candidate['recipe']['mode'] == 'update' else '')
            row.phase = '检查完成，等待执行'
            row.execution = _json(_accepted_execution(row.id, label=row.phase))
            session.add(row)
            session.commit()
            session.refresh(row)
            key = row.id
        runner._clear_chain(project, '切换到明确确认的全流程')
        _launch(key, uid)
    return get(key)


def _launch(key, uid):
    event = threading.Event()
    _running[key] = event
    _leases[uid] = key
    try:
        _pool.submit(_execute, key, event)
    except Exception as exc:
        # 受理回执仍保留；队列拒绝不能留下永久租约，也不能把重送变成重复执行。
        if _running.get(key) is event:
            _running.pop(key, None)
        if _leases.get(uid) == key:
            _leases.pop(uid, None)
        from dubforge.execution_progress import Recorder
        message = '完整流程未能加入后台队列，请重新检查后继续；已有结果和回执保留'
        execution = Recorder(previous=get(key).get('execution'), owner='workflow', run_id=key).finish('failed', message)
        _update(key, status='failed', phase='后台队列不可用，等待重新检查', error=message, execution=execution)
        raise WorkflowError(message, code='WORKFLOW_QUEUE_UNAVAILABLE') from exc


def _check_cancel(event):
    if event.is_set():
        raise WorkflowError('全流程已停止，已完成结果保留；不会自动继续投稿', code='WORKFLOW_STOPPED')


_LEGACY_STOPPING_PHASE = '正在停止；已发送的投稿将等待回执'


def _legacy_stopped_phase(run, *, receipt=None):
    """旧版本已收尾的停止文案只在读取时修正，不回写历史状态或过程。"""
    if run.get('status') == 'paused' and run.get('phase') == _LEGACY_STOPPING_PHASE:
        return _stopped_phase(run, receipt=receipt)
    return run.get('phase', '')


def _stopped_phase(run, *, receipt=None):
    """本地协调器退出不能证明远端请求撤回；只解释已保存的稿件事实。"""
    task = run.get('publication_update')
    pub = task or run.get('publication') or {}
    if pub.get('status') == 'unknown':
        return '自动推进已暂停；原稿更新结果待核对' if task else '自动推进已暂停；投稿结果待核对'
    if task:
        return '原稿更新自动推进已暂停；请核对已有请求与回执'
    if run.get('publication_id'):
        known_receipt = receipt if receipt is not None else pub.get('receipt') or {}
        if known_receipt.get('bvid'):
            return '已停止发布后处理；已保留投稿回执'
        if pub.get('status') == 'draft':
            return '制作已停止，发布草稿已保留'
        return '自动推进已暂停；已有稿件与远端回执待核对'
    return '制作已停止，已完成结果保留'


def _check_configuration(run, steps=None):
    # 全流程只冻结目标、版本和人工输入范围；每个制作步骤真正开始时
    # 重新读取当前设置并由 Runner 写入该 Job 的执行快照。这样用户可以
    # 在等待后续步骤时调整参数，不会把尚未运行的步骤误判为冲突。
    from dubforge.workflow_plan import configuration_snapshot
    stored = run['plan'].get('configuration_snapshot')
    if not isinstance(stored, dict):
        return
    # 源视频身份属于流程范围而非步骤参数，变化时仍需重新确认，避免把
    # 另一份视频投稿到同一流程记录。
    current = configuration_snapshot(Dependencies(crud.get_project(run['project_uid'])),
                                     version_mode='version_specs' in run['plan'], steps=[],
                                     legacy=stored.get('schema') != 2)
    if stored.get('source') != current.get('source'):
        raise WorkflowError('流程使用的视频来源已变化，请重新预检后继续', code='WORKFLOW_SOURCE_CHANGED')


def _wait_step(uid, key, event):
    from dubforge.runner import runner
    project = crud.get_project(uid)
    cancelled = False
    while runner.current_job_id(project.id, key) is not None:
        if event.is_set() and not cancelled:
            runner.cancel_step(uid, key)
            cancelled = True
        time.sleep(.1)
    _check_cancel(event)
    step = crud.get_step(project.id, key)
    if step.status != 'done':
        raise WorkflowError(f'{key} 未完成：{step.error_message or "请查看步骤状态"}', code='WORKFLOW_STEP_FAILED')


def _comments(run, event, progress=None):
    uid, spec = run['project_uid'], run['recipe']
    # 采集/评分/翻译/挑选与手动链的自动准备共用同一段实现 (comments_auto)。
    # 共享一组上屏评论时，还需满足本次各分P冻结的语言设置，避免到合成才发现超长。
    from dubforge import comments_auto
    # 评论设置属于已确认的运行配方。流程排队或恢复期间即使项目设置发生变化，
    # 也不能把新旧设置混入同一份评分/翻译/上屏方案；用户需要重新预检才能采用新值。
    base = spec['comment_settings']
    display_settings = [
        {**base, 'language_mode': version.get('values', {}).get('comments.language_mode', base['language_mode'])}
        for version in run.get('plan', {}).get('version_specs', []) if version['include_comments'] and not version.get('update') and not version.get('timeline')]
    def report(label):
        _update(run['id'], phase=label)
        if progress:
            progress.phase('comments.prepare', label)
    try:
        warnings, _count = comments_auto.prepare_overlay(
            uid, spec, cancel_check=lambda: _check_cancel(event), extra_settings=display_settings,
            phase=report)
    except DubForgeError as exc:
        if exc.code == 'COMMENTS_AUTO_EMPTY':
            raise WorkflowError(exc.message, code='WORKFLOW_COMMENTS_EMPTY')
        raise
    for warning in warnings:
        run['plan']['warnings'].append(warning)
        _update(run['id'], plan=run['plan'])


def _check_dubbing(run):
    specs = run['plan'].get('version_specs')
    from dubforge.pipeline.assembly_versions import requires_dubbing
    needs_dubbing = (any(requires_dubbing(v) for v in specs) if specs is not None
                     else any(dimensions(v)['dubbing'] for v in run['recipe']['variants']))
    if needs_dubbing:
        Dependencies(crud.get_project(run['project_uid'])).require_dubbing()


def _bind_assembled_versions(run, specs, result):
    """发布只消费这次实际成功的版本和产物，不重新套用确认时的旧定义。"""
    from dubforge.pipeline import assembly_versions, version_render
    project = crud.get_project(run['project_uid'])
    deps = Dependencies(project)
    if any(assembly_versions.requires_dubbing(version) for version in specs):
        deps.require_dubbing()
    job = crud.latest_job(project.id, 'assemble')
    if result.get('job_id') is not None:
        if not job or job.id != result['job_id'] or job.status != 'done':
            raise WorkflowError('本次合成任务尚未成功，不能准备发布资料', code='WORKFLOW_OUTPUT_STALE')
        actual = json.loads(job.task_data).get('version_specs')
        if isinstance(actual, list) and [row.get('id') for row in actual] == [row['id'] for row in specs]:
            specs = actual
    records = assembly_versions.results(project.uid)
    bound = []
    for version in specs:
        if version_render.uses_source_directly(deps.for_version(version), version):
            artifact = deps.arts.get('download/video.mp4') if _is_update(run) else None
            if _is_update(run):
                path = service._eligible_file(artifact, project, deps.root) if artifact else None
                if (not artifact or not artifact.sha256 or not path or not path.is_file()
                        or not deps.file_matches('download', 'video.mp4')):
                    raise WorkflowError('原片文件与项目登记不一致，不能准备替换视频', code='WORKFLOW_OUTPUT_STALE')
            bound.append(dict(version=copy.deepcopy(version), artifact_id=artifact.id if artifact else None,
                              artifact_sha256=artifact.sha256 if artifact else ''))
            continue
        record = records.get(version['id'])
        artifact = assembly_versions.result_artifact(deps, version, records)
        if (not record or not artifact or artifact.sha256 != record.artifact_hash
                or not assembly_versions.is_current(deps, version)):
            raise WorkflowError(f'本次合成版本无法核验：{version["name"]}，不会选用旧成片', code='WORKFLOW_OUTPUT_STALE')
        bound.append(dict(version=json.loads(record.snapshot), artifact_id=artifact.id,
                          artifact_sha256=artifact.sha256))
    run['plan']['assembled_versions'] = bound
    _update(run['id'], plan=run['plan'])


def _prepare_publication(run, event, progress=None):
    _check_dubbing(run)
    uid, spec = run['project_uid'], run['recipe']
    # 文案先于草稿保存生成，但对话仍绑定最终稿件 ID，发布页才能按稿件统一检索。
    publication_key = run['plan'].get('refresh_publication') or 'workflow-' + run['id']
    settings = copy.deepcopy(run['plan']['settings'])
    if progress:
        progress.phase('publish.files.inspect', '读取本次制作结果')
    data = service.candidates(uid)
    # 显式项目/模板值优先，视频资料只补空项；AI不替用户决定分区或权限。
    for key, value in data['suggestion'].items():
        if not settings.get(key) and run['plan']['origins'].get(key) == '默认':
            settings[key] = value
    copy_fields = automatic_copy_fields(run['plan']['origins'])
    if spec['ai_copy'] and copy_fields:
        from dubforge.publishing.copywriting import generate
        generated = generate(crud.get_project(uid), settings, '',
                             cancel_check=lambda: _check_cancel(event), progress=progress,
                             conversation_owner_id=publication_key)['values']
        for key in copy_fields:
            settings[key] = generated[key]
    _check_cancel(event)
    # 制作已完成: 发布资料使用计划冻结的设置快照, 只校验来源身份, 不再被制作参数修改打断。
    _check_configuration(run, [])
    if spec['cover_source'] == 'platform':
        if progress:
            progress.phase('publish.cover.fetch', '获取平台封面', state='waiting')
        cover_id = service.platform_cover(uid)['cover_id']
    else:
        if progress:
            progress.phase('publish.cover.validate', '核验指定封面')
        cover_id = spec['cover_id']
        if service._file_hash(service._cover_path(cover_id)) != run['plan']['cover_hash']:
            raise WorkflowError('确认后的封面已变化，请重新选择')
    parts = []
    if progress:
        progress.phase('publish.files.bind', '核对实际成片与分P顺序')
    for variant in (spec['version_ids'] if spec.get('version_ids') is not None else spec['variants']):
        if spec.get('version_ids') is not None:
            from dubforge.pipeline import assembly_versions, version_render
            project = crud.get_project(uid)
            deps = Dependencies(project)
            binding = next((row for row in run['plan'].get('assembled_versions', [])
                            if row['version']['id'] == variant), None)
            if binding:
                version = copy.deepcopy(binding['version'])
            else:
                # 不执行 assemble 的复用流程也采用本轮实际设置，不能取较早确认的覆盖值。
                definition = next((row for row in assembly_versions.catalog(uid)['versions'] if row['id'] == variant), None)
                if definition is None:
                    raise WorkflowError('所选成片版本已移除，请重新检查', code='WORKFLOW_OUTPUT_STALE')
                overlay = comment_studio.effective_overlay(uid, comment_studio._read(project).get('overlay'))
                version = assembly_versions.resolve(deps, definition, comment_plan=overlay,
                    comment_settings=spec['comment_settings'] if spec['comments_mode'] == 'auto' else None)
            original = version_render.uses_source_directly(deps.for_version(version), version)
            artifact = None if original else assembly_versions.result_artifact(deps, version)
            if binding and not original and (not artifact or artifact.id != binding['artifact_id']
                                             or artifact.sha256 != binding['artifact_sha256']):
                raise WorkflowError('本次成功成片已被替换，请重新检查；不会上传其他文件', code='WORKFLOW_OUTPUT_STALE')
            if not original and (not artifact or not assembly_versions.is_current(deps, version)):
                raise WorkflowError(f'指定成片版本仍需更新：{version["name"]}，请检查制作步骤；不会上传旧成片',
                                    code='WORKFLOW_OUTPUT_STALE')
            file = next((f for f in data['files'] if f['exists'] and
                         (f['is_source'] if original else f['id'] == artifact.id)), None)
            if not file: raise WorkflowError(f'缺少指定成片版本：{version["name"]}，不会选用其他版本')
            parts.append(dict(artifact_id=file['id'], title=spec['part_titles'].get(variant, version['name'])))
            continue
        file = next((f for f in data['files'] if f['exists'] and
                     (f['is_source'] if variant == 'src' else final_variant(f['name']) == variant)), None)
        if file is None:
            raise WorkflowError(f'缺少本次指定成品：{LABELS[variant]}，不会选用其他版本')
        parts.append(dict(artifact_id=file['id'], title=spec['part_titles'].get(variant, LABELS[variant])))
    _check_cancel(event)
    # 每个流程使用固定草稿 ID；保存成功才绑定，进程中断重试不会创建第二份稿件。
    if run['plan'].get('refresh_publication'):
        pub = service.view(publication_key)
        binding = run['plan'].get('publication_binding') or {}
        if (pub['project_uid'] != uid or pub['status'] != 'draft' or pub['receipt'].get('bvid')
                or pub.get('archived_at') or pub.get('deleted_at')
                or pub['revision'] != binding.get('revision') or digest(pub['draft']) != binding.get('fingerprint')):
            raise WorkflowError('确认后的草稿资料已变化，请重新预检；不会创建第二份稿件', code='WORKFLOW_PLAN_CHANGED')
    else:
        pub = service.create(uid, key=publication_key)
    if progress:
        progress.phase('publish.draft.save', '保存已核验的发布草稿')
    pub = service.save(publication_key, dict(revision=pub['revision'], account_id=spec['account_id'], template_id='',
        cover_id=cover_id, overrides=settings, parts=parts,
        chapter_options=service._chapter_options(dict(enabled=spec.get('chapters_enabled', False),
            translate=spec.get('chapters_translate', True), permanent=spec.get('chapters_permanent', True),
            instruction=spec.get('chapters_instruction', ''),
            items=data.get('chapters', [])))))
    _update(run['id'], publication_id=publication_key)
    return pub


def _wait_publication(key, event):
    stop_sent = False
    while key in service._running:
        if event is not None and event.is_set() and not stop_sent:
            try:
                service.pause(key)
                stop_sent = True
            except service.PublishError:
                pass  # 已发送的投稿不能撤回，等回执落盘后再停止后续动作。
        time.sleep(.1)
    return service.view(key)


def _execute(key, event):
    run = get(key)
    uid = run['project_uid']
    from dubforge.execution_progress import Recorder
    progress = Recorder(lambda value: _update(key, execution=value), previous=run.get('execution'),
                        owner='workflow', run_id=key)
    try:
        with _owned(key):
            _check_cancel(event)
            _update(key, status='running', error='')
            progress.phase('workflow.prepare', '开始已确认的完整流程')
            updating = _is_update(run)
            if (updating and not _update_task(run)) or (not updating and
                    (not run['publication_id'] or run['plan'].get('refresh_publication'))):
                from dubforge.runner import runner
                for index, step in enumerate(run['plan']['steps']):
                    if index < run['cursor']:
                        continue
                    _check_cancel(event)
                    _check_configuration(run, run['plan']['steps'][index:])
                    _update(key, phase=run['plan']['labels'][step])
                    progress.phase('production.' + step, run['plan']['labels'][step],
                                   completed=index, total=len(run['plan']['steps']), unit='制作步骤')
                    if step == 'comments':
                        _comments(run, event, progress=progress)
                    elif step == 'assemble':
                        _check_dubbing(run)
                        state = comment_studio.view(uid)
                        comment_revision = (run['plan']['overlay_revision'] if run['recipe']['comments_mode'] == 'saved' else state['overlay_revision'])
                        if 'version_specs' in run['plan']:
                            from dubforge.pipeline import assembly_versions
                            # 只冻结流程确认的版本选择；版本覆盖与全局/项目制作
                            # 设置在 assemble 真正拿到执行槽位时重新解析。
                            project = crud.get_project(uid)
                            deps = Dependencies(project)
                            current_catalog = {v['id']: v for v in assembly_versions.catalog(uid)['versions']}
                            specs = []
                            for stored in run['plan']['version_specs']:
                                definition = current_catalog.get(stored.get('id'))
                                if definition is None:
                                    definition = {key: stored[key] for key in (
                                        'id', 'name', 'dubbing', 'burn_subtitles',
                                        'include_comments', 'overrides', 'partial', 'update', 'timeline') if key in stored}
                                specs.append(assembly_versions.resolve(deps, definition))
                            overlay = comment_studio.for_task(uid, any(v['include_comments'] and not v.get('update') and not v.get('timeline') for v in specs), comment_revision)
                            for v in specs:
                                if v['include_comments'] and not v.get('update') and not v.get('timeline'):
                                    v['comment_overlay'] = {**copy.deepcopy(overlay), 'settings': comment_studio.validate_settings({**overlay['settings'],
                                        **{k.removeprefix('comments.'): value for k, value in v['values'].items() if k.startswith('comments.')}})}
                            result = runner.start_versions(uid, {}, frozen={'versions': specs})
                        else:
                            variants = [v for v in run['recipe']['variants'] if v != 'src']
                            needs_comments = any(dimensions(v)['include_comments'] for v in variants)
                            runner.start_assemble_outputs(uid, outputs=[dimensions(v) for v in variants],
                                                          overlay_revision=comment_revision if needs_comments else None)
                        _wait_step(uid, step, event)
                        if 'version_specs' in run['plan'] and run['recipe']['mode'] != 'produce':
                            _bind_assembled_versions(run, specs, result)
                    else:
                        if step == 'tts':
                            from dubforge.pipeline.dubbing_policy import normalize
                            policy = normalize(run['recipe'].get('dubbing'))
                            result = runner.start_step(uid, step, dubbing=policy,
                                                       dubbing_operation_id=run['id'])
                        else:
                            result = runner.start_step(uid, step)
                        if result['status'] == 'started':
                            _wait_step(uid, step, event)
                    _update(key, cursor=index + 1)
                _check_cancel(event)
                if run['plan']['steps']:
                    progress.phase('production.completed', '制作步骤已完成', completed=len(run['plan']['steps']),
                                   total=len(run['plan']['steps']), unit='制作步骤')
                if run['recipe']['mode'] == 'produce':
                    _update(key, status='completed', phase='制作完成')
                    progress.finish('completed', '本次制作范围已完成')
                    return
                if not updating:
                    _update(key, phase='准备发布资料')
                    pub = _prepare_publication(run, event, progress=progress)
            if updating:
                _check_cancel(event)
                from dubforge import workflow_update
                _update(key, phase='准备更新原稿件')
                if run['plan'].pop('_update_reconcile_once', False):
                    _update(key, plan=run['plan'])
                    from dubforge.publishing import updates
                    task = _update_task(run)
                    if not task:
                        raise WorkflowError('原稿更新任务不存在，请重新检查', code='WORKFLOW_UPDATE_NOT_FOUND')
                    task = updates.action(task['id'], dict(action='reconcile', confirmed=True,
                        fingerprint=task['fingerprint']), workflow_id=key)
                else:
                    task = workflow_update.deliver(run, event, progress)
                if event.is_set():
                    from dubforge.publishing import updates
                    updates.action(task['id'], dict(action='pause'))
                    _update(key, status='attention', phase=_stopped_phase(get(key)),
                            post_processing_paused=True)
                    return
                _sync_update_task(key, task)
                return
            if run['publication_id'] and not run['plan'].get('refresh_publication'):
                pub = service.view(run['publication_id'])
            if run['recipe']['mode'] == 'draft':
                _update(key, status='ready', phase='发布草稿已准备，可前往发布页核对')
                progress.finish('completed', '发布草稿已准备，可核对后投稿')
                return
            _check_cancel(event)
            if pub['status'] == 'unknown':
                raise WorkflowError('投稿结果待核对，请到发布页关联回执；禁止自动重投')
            if run['recipe']['mode'] == 'upload':
                if pub['receipt'].get('bvid'):
                    raise WorkflowError('此上传任务已关联投稿回执，请到发布页核对，不再发送上传或投稿')
                if pub['status'] != 'uploaded':
                    _update(key, phase='上传选定文件，完成后等待确认')
                    progress.phase('publish.upload.preflight', '核验文件后仅上传，不提交新稿')
                    preview = service.plan(pub['id'], progress=progress)
                    service.enqueue(pub['id'], dict(action='upload', confirmed=True, revision=pub['revision'],
                        fingerprint=preview['fingerprint'], acknowledge_ai_notice=True))
                    pub = _wait_publication(pub['id'], event)
                if pub['status'] != 'uploaded':
                    raise WorkflowError(pub['error'] or '上传尚未完成，请到发布页继续上传；不会自动投稿')
                _update(key, status='ready', phase='已上传，等待确认投稿', error='')
                progress.finish('completed', '文件已上传，需在发布页明确确认后才投稿')
                return
            if not pub['receipt'].get('bvid'):
                _update(key, phase='上传并投稿')
                progress.phase('publish.preflight', '核验发布文件与内容审查')
                preview = service.plan(pub['id'], progress=progress)
                action = 'submit' if pub['status'] == 'uploaded' else 'publish'
                service.enqueue(pub['id'], dict(action=action, confirmed=True, revision=pub['revision'],
                    fingerprint=preview['fingerprint'], acknowledge_ai_notice=True))
                pub = _wait_publication(pub['id'], event)
            if pub['receipt'].get('bvid'):
                if event.is_set():
                    phase = _stopped_phase(get(key))
                    _update(key, status='attention', phase=phase,
                            post_processing_paused=True)
                    progress.finish('paused', phase, label=phase)
                    return
                _update(key, status='monitoring', phase='已投稿，等待审核与合集核对', next_check=time.time())
                progress.phase('publish.review.waiting', '已投稿，等待平台审核', state='waiting', next_check_at=time.time())
            else:
                raise WorkflowError(pub['error'] or '发布未完成，请核对后继续')
    except Exception as exc:
        message = exc.message if isinstance(exc, DubForgeError) else '全流程未完成，请查看对应步骤和发布记录'
        if event.is_set():
            phase = _stopped_phase(get(key))
            _update(key, status='paused', phase=phase, error=message)
            progress.finish('paused', message, label=phase)
        else:
            _update(key, status='failed', error=message)
            progress.finish('failed', message)
    finally:
        with _lock:
            _running.pop(key, None)
            if _leases.get(uid) == key:
                _leases.pop(uid, None)


def _resume_identity(body):
    """仅保存请求内容的摘要，兼容没有请求标识的早期调用。"""
    if 'request_id' not in body:
        return None, None
    request = body['request_id']
    if not isinstance(request, str) or len(request) != 36:
        raise WorkflowError('继续任务的请求标识无效，请重新检查')
    try:
        request = str(uuid.UUID(request))
    except ValueError as exc:
        raise WorkflowError('继续任务的请求标识无效，请重新检查') from exc
    return request, digest({key: body.get(key) for key in ('recipe', 'fingerprint', 'recovery_fingerprint')})


def _resume_replayed(run, request, signature):
    requests = run['plan'].get('_resume_requests') or {}
    previous = requests.get(request) if request and isinstance(requests, dict) else None
    if previous is None:
        return False
    if not isinstance(previous, dict) or previous.get('signature') != signature:
        raise WorkflowError('同一恢复请求标识不能提交不同方案，请重新检查', code='WORKFLOW_CONFLICT')
    return True


def _resume_receipts(run, request, signature, candidate):
    """所有已接受的恢复请求永久保留，重新规划不能丢失幂等回执。"""
    requests = copy.deepcopy(run['plan'].get('_resume_requests') or {})
    if not isinstance(requests, dict):
        requests = {}
    if request:
        requests[request] = dict(signature=signature, fingerprint=candidate.get('fingerprint'),
            recovery_fingerprint=candidate.get('recovery_fingerprint'),
            accepted_state=dict(status=run['status'], cursor=run['cursor'], publication_id=run['publication_id']))
    if requests:
        candidate['_resume_requests'] = requests
    return candidate


def _resume_update_task(key, body, request, signature):
    """恢复固定更新任务也与手动制作共用启动锁，避免确认和租约之间出现竞态。"""
    from dubforge.project_archive import assert_active
    from dubforge.runner import runner
    from dubforge.pipeline import dubbing_session
    with service._lock, runner._start_lock, dubbing_session.gate, video_info.gate, _lock:
        run = get(key)
        if _resume_replayed(run, request, signature):
            return run
        if run['status'] not in {'paused', 'failed', 'attention'} or key in _running:
            raise WorkflowError('当前任务不能继续')
        task = _update_task(run)
        if not task:
            raise WorkflowError('原稿更新任务已变化，请重新检查', code='WORKFLOW_PLAN_CHANGED')
        project = assert_active(run['project_uid'])
        check_idle(project.uid)
        video_info.check_idle(project.uid)
        dubbing_session.check_idle(project.uid)
        if any(step.status in {'running', 'queued'} for step in crud.get_steps(project.id)):
            raise WorkflowError('项目仍有步骤运行或收尾，请稍后继续')
        candidate = copy.deepcopy(run['plan'])
        candidate['recovery'], candidate['recovery_fingerprint'] = _update_recovery(run, task)
        if (body.get('fingerprint') != candidate['fingerprint']
                or body.get('recovery_fingerprint') != candidate['recovery_fingerprint']):
            raise WorkflowError('原稿更新状态或确认范围已变化，请重新检查后继续', code='WORKFLOW_PLAN_CHANGED')
        candidate = _resume_receipts(run, request, signature, candidate)
        if task['status'] == 'unknown':
            candidate['_update_reconcile_once'] = True
        _update(key, plan=candidate, status='queued', phase='继续原稿更新', error='',
                post_processing_paused=False,
                execution=_accepted_execution(key, run.get('execution'), label='已受理，等待继续原稿更新'))
        _launch(key, project.uid)
    return get(key)


def resume(key, body):
    from dubforge.project_archive import assert_active
    if body.get('confirmed') is not True:
        raise WorkflowError('继续前需要重新确认流程范围')
    request, signature = _resume_identity(body)
    current = get(key)
    if _is_update(current) and _update_task(current):
        return _resume_update_task(key, body, request, signature)
    with _lock:
        run = get(key)
        if _resume_replayed(run, request, signature):
            return run
        if run['status'] not in {'paused', 'failed', 'attention'} or key in _running:
            raise WorkflowError('当前任务不能继续')
        if run['publication_id'] and not _is_update(run):
            pub = service.view(run['publication_id'])
            if pub['status'] == 'unknown':
                raise WorkflowError('请先在发布页核对投稿回执，不会自动重投')
            if pub['receipt'].get('bvid'):
                candidate = _resume_receipts(run, request, signature, copy.deepcopy(run['plan']))
                _update(key, plan=candidate, status='monitoring', phase='继续核对平台状态', next_check=time.time(),
                        check_failures=0, error='', post_processing_paused=False,
                        execution=_accepted_execution(key, run.get('execution'), label='继续核对平台状态',
                            phase='publish.review.waiting', state='waiting'))
                resume_post_processing(run['publication_id'])
                return get(key)
            # 已绑定发布文件的任务不重新制作；必须到发布页显式确认当前资料后继续。
            raise WorkflowError('制作已完成，请到发布页核对现有草稿或已上传文件并继续，不能新建重复稿件')
    assert_active(run['project_uid'])
    candidate = _plan(run['project_uid'], {**(body.get('recipe') or {}), 'resume_id': key})
    if candidate['blockers'] or body.get('fingerprint') != candidate['fingerprint']:
        raise WorkflowError('预检未通过或已失效，请重新预检', code='WORKFLOW_PLAN_CHANGED')
    from dubforge.runner import runner
    from dubforge.pipeline import dubbing_session
    with service._lock, runner._start_lock, dubbing_session.gate, video_info.gate, _lock:
        run = get(key)
        if _resume_replayed(run, request, signature):
            return run
        if key in _running or run['status'] not in {'paused', 'failed', 'attention'}:
            raise WorkflowError('此流程已经在继续或状态已变化')
        if run['publication_id'] and not _is_update(run):
            raise WorkflowError('此流程已进入发布阶段，请到发布页继续现有稿件', code='WORKFLOW_PLAN_CHANGED')
        check_idle(run['project_uid'])
        project = assert_active(run['project_uid'])
        if any(s.status in {'running', 'queued'} for s in crud.get_steps(project.id)):
            raise WorkflowError('项目仍有步骤运行或收尾，请稍后继续')
        service.check_project_idle(project.uid)
        video_info.check_idle(project.uid)
        dubbing_session.check_idle(project.uid)
        current = _plan(project.uid, {**candidate['recipe'], 'resume_id': key}, online=False,
                        previous_update=candidate.get('update_plan'))
        if current['fingerprint'] != candidate['fingerprint']:
            raise WorkflowError('预检期间设置已变化，请重新确认', code='WORKFLOW_CONFLICT')
        if current['blockers']:
            raise WorkflowError('恢复检查期间依赖或资料已变化，请重新检查：' + '；'.join(current['blockers']),
                                code='WORKFLOW_PLAN_CHANGED')
        # 文件或已完成状态在预检后改变也会改变恢复范围，不能仅比较制作配置。
        current, cursor = _recovery_plan(run, current, project)
        if 'recovery_fingerprint' in body and body['recovery_fingerprint'] != current['recovery_fingerprint']:
            raise WorkflowError('恢复起点或步骤已变化，请重新检查后确认', code='WORKFLOW_PLAN_CHANGED')
        # 在线服务预检的警告仍保留，实际步骤、游标和说明使用锁内的最新解析。
        candidate = {**candidate, **{name: current[name] for name in ('steps', 'labels', 'recovery', 'recovery_fingerprint')}}
        candidate = _resume_receipts(run, request, signature, candidate)
        _update(key, recipe=candidate['recipe'], plan=candidate, cursor=cursor, status='queued',
                phase='恢复检查完成，等待执行', error='', post_processing_paused=False,
                execution=_accepted_execution(key, run.get('execution'), label='恢复检查完成，等待执行'))
        runner._clear_chain(project, '恢复全流程')
        _launch(key, project.uid)
    return get(key)


def stop(key):
    update_task = None
    with _lock:
        run = get(key)
        if _is_update(run):
            update_task = _update_task(run)
        if event := _running.get(key):
            event.set()
            phase = ('正在停止，核对已有稿件与请求回执' if run.get('publication_id') or update_task
                     else '正在停止，等待当前制作安全收尾')
            _update(key, status='stopping', phase=phase, post_processing_paused=True)
        elif run['status'] == 'monitoring':
            _update(key, status='attention', phase=_stopped_phase(run), post_processing_paused=True)
    if update_task:
        from dubforge.publishing import updates
        updates.action(update_task['id'], dict(action='pause'))
    return get(key)


def post_processing_paused(publication_id):
    """共同调度门禁；诊断进度不能替代这项持久授权状态。"""
    with Session(get_engine()) as session:
        return session.exec(select(WorkflowRun.id).where(WorkflowRun.publication_id == publication_id,
            WorkflowRun.post_processing_paused.is_(True))).first() is not None


def resume_post_processing(publication_id):
    """仅显式继续/章节确认入口解除停止，不由后台扫描擅自恢复。"""
    with _lock, Session(get_engine()) as session:
        rows = session.exec(select(WorkflowRun).where(WorkflowRun.publication_id == publication_id,
            WorkflowRun.post_processing_paused.is_(True))).all()
        for row in rows:
            row.post_processing_paused = False
            if row.status == 'attention':
                row.status, row.phase, row.next_check, row.error = 'monitoring', '继续发布后处理', time.time(), ''
            session.add(row)
        session.commit()


def track_confirmed_delivery(publication_id, *, update_id=''):
    """发布页另行明确提交后跟踪同一回执；保留原任务冻结的交付终点。"""
    with _lock, Session(get_engine()) as session:
        rows = session.exec(select(WorkflowRun).where(WorkflowRun.publication_id == publication_id,
                                                     WorkflowRun.status == 'ready')).all()
        for row in rows:
            spec, candidate = json.loads(row.recipe), json.loads(row.plan)
            if spec.get('mode') == 'update':
                if not update_id or (candidate.get('publication_update_id') or 'workflow-update-' + row.id) != update_id:
                    continue
            elif update_id:
                continue
            row.status, row.phase, row.next_check = 'monitoring', '人工确认提交，继续核对发布结果', time.time() + 3
            row.error, row.post_processing_paused = '', False
            session.add(row)
        session.commit()


def monitor_once():
    from dubforge import release_batches
    from dubforge.publishing import chapter_dispatch
    chapter_dispatch.scan()
    with Session(get_engine()) as session:
        due = [row.id for row in session.exec(select(WorkflowRun).where(
            WorkflowRun.status == 'monitoring', WorkflowRun.next_check <= time.time())).all()]
    for key in due:
        run = get(key)
        if run['status'] != 'monitoring' or run.get('post_processing_paused'):
            continue
        if _is_update(run):
            try:
                task = _update_task(run)
                if not task:
                    raise WorkflowError('缺少原稿更新任务，请核对后继续')
                _sync_update_task(key, task)
            except Exception as exc:
                _update(key, status='attention', error=getattr(exc, 'message', '原稿更新状态读取失败，请稍后核对'),
                        phase='原稿更新待核对', next_check=0)
            continue
        # 顺序发布批次在审核等待阶段拥有唯一的稿件核对权；避免此处
        # 同时 enqueue(check) 与批次的 refresh_status 交错写回回执。
        if release_batches.owns_publication(run['publication_id']):
            continue
        _update(key, next_check=time.time() + 60)
        try:
            pub = service.view(run['publication_id'])
            if pub['status'] in service.ACTIVE or pub['id'] in service._running:
                continue
            if not pub['receipt'].get('bvid'):
                raise WorkflowError('缺少投稿回执，请前往发布页核对')
            if _finish_publication_monitor(key, pub):
                continue
            # 仅自动查询；合集重试必须显式确认，避免写请求结果不明时盲重发。
            if pub['status'] == 'collection_pending':
                _update(key, status='attention', phase='视频已投稿，合集待处理', error=pub['error'])
                continue
            # 批次仍占用项目时，监控只以该真实流程身份读取回执，不扩大成投稿授权。
            with _owned(key):
                service.enqueue(pub['id'], dict(action='check', revision=pub['revision']))
                pub = _wait_publication(pub['id'], None)
            if get(key)['status'] != 'monitoring':
                continue
            if pub['error']:
                raise WorkflowError(pub['error'])
            if _finish_publication_monitor(key, pub):
                pass
            elif pub['status'] in {'rejected', 'unknown', 'collection_pending'}:
                _update(key, status='attention', phase='平台结果需要处理', error=pub['receipt'].get('reject_reason') or pub['error'])
            else:
                _update(key, phase='已投稿，等待平台审核', next_check=time.time() + 120, check_failures=0, error='')
        except Exception as exc:
            if get(key)['status'] != 'monitoring':
                continue
            failures = run['check_failures'] + 1
            _update(key, check_failures=failures, status='attention' if failures >= 3 else 'monitoring',
                    error=getattr(exc, 'message', '平台状态查询失败，请稍后手动核对'), next_check=time.time() + 120 * failures)


def _finish_publication_monitor(key, pub):
    """稿件交付与章节交付分别展示，章节重试不进入投稿状态机。"""
    state = pub.get('chapter_sync') or {}
    if state.get('status') == 'superseded':
        # 原上传回执仍然有效；后续换片有自己的审核和章节任务，旧流程到此交接。
        # 必须先于当前远端状态检查，否则会重复跟踪新视频，甚至把新退回当成旧上传失败。
        from dubforge.execution_progress import Recorder
        progress = Recorder(previous=state.get('execution') or pub.get('execution'), owner='workflow', run_id=key)
        execution = progress.phase('publish.update.handoff', '原稿后续处理已交接', state='completed',
            detail='原稿上传回执保留；新成片的审核、章节和合集请在“更新原稿件”任务中查看和继续。')
        _update(key, status='completed', phase='原稿已投稿，章节已交由原稿更新任务',
                next_check=0, check_failures=0, error='', execution=execution)
        return True
    if pub['status'] not in {'published', 'private', 'reviewed'}:
        return False
    options = pub.get('snapshot', {}).get('chapter_options') or {}
    execution = pub.get('execution') or {}
    if options.get('enabled') and options.get('items'):
        status = state.get('status')
        execution = state.get('execution') or execution
        if status in {'failed', 'unknown'}:
            _update(key, status='attention', phase='视频已发布，章节待处理',
                    error=state.get('error') or '请在发布页处理章节，无需重新投稿', execution=execution)
            return True
        if status not in {'completed', 'skipped'}:
            _update(key, phase='视频已发布，章节处理中', next_check=time.time() + 10,
                    check_failures=0, error='', execution=execution)
            return True
    _update(key, status='completed', phase='已发布' if pub['status'] == 'published'
            else '审核已通过 · 仅自己可见' if pub['status'] == 'private' else '修改已生效', error='', execution=execution)
    return True


def chapter_status_changed(publication_id):
    """后台章节重试后只重新核对原稿件，不创建新流程或上传。"""
    with _lock, Session(get_engine()) as session:
        for row in session.exec(select(WorkflowRun).where(WorkflowRun.publication_id == publication_id,
                WorkflowRun.status == 'attention', WorkflowRun.phase == '视频已发布，章节待处理')).all():
            row.status, row.next_check, row.error = 'monitoring', time.time(), ''
            session.add(row)
        session.commit()


def recover():
    with _lock, Session(get_engine()) as session:
        for row in session.exec(select(WorkflowRun).where(WorkflowRun.status.in_(BUSY))).all():
            row.status = 'paused'
            row.error = '程序重启，全流程已暂停；确认后继续，不会自动上传或投稿'
            session.add(row)
        session.commit()


def start_monitor():
    global _monitor
    if _monitor and _monitor.is_alive():
        return
    _monitor_stop.clear()
    def watch():
        while not _monitor_stop.wait(2):
            try:
                monitor_once()
            except Exception:
                pass  # 数据库关闭/启动中的短暂异常不应触发任何重投。
    _monitor = threading.Thread(target=watch, daemon=True, name='workflow-monitor')
    _monitor.start()


def shutdown():
    _monitor_stop.set()
    with _lock:
        for event in _running.values():
            event.set()
    if _monitor:
        _monitor.join(timeout=3)
