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
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.assembly_outputs import final_variant, dimensions
from dubforge.publishing import service
from dubforge.workflow_models import WorkflowRun
from dubforge.workflow_plan import LABELS, WorkflowError, configuration_stamp, plan, recipe

_lock = threading.RLock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='workflow')
_running = {}
_leases = {}
_owner = threading.local()
_monitor_stop = threading.Event()
_monitor = None
BUSY = {'queued', 'running', 'stopping'}
OPEN = BUSY | {'monitoring'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def check_idle(uid):
    # 不反向获取启动/编辑锁；调用方在自己的事务锁内核对租约。
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
    for field in ('recipe', 'plan'):
        data[field] = json.loads(data[field])
    if row.publication_id:
        try:
            pub = service.view(row.publication_id)
            data['publication'] = {k: pub[k] for k in ('id', 'status', 'progress', 'error', 'receipt')}
        except service.PublishError:
            pass
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
            setattr(row, name, _json(value) if name in {'recipe', 'plan'} else value)
        session.add(row)
        session.commit()


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
            run = dict(id=row.id, status=row.status, mode=spec.get('mode'), phase=row.phase,
                       cursor=row.cursor, total=len(steps), error=row.error, publication_id=row.publication_id)
            if row.publication_id and (pub := session.get(Publication, row.publication_id)):
                run['publication'] = dict(id=pub.id, status=pub.status, progress=pub.progress, error=pub.error)
        return dict(mode=settings['values']['run.mode'], mode_source=settings['sources']['run.mode'], run=run,
                    existing_publication_id=previous.publication_id if previous else '')


def _availability(uid, mode, resume_id=None):
    """预检提前解释执行阻塞；启动锁内仍保留最终校验，不能以页面状态代替。"""
    issues = []
    if resume_id is not None and (not isinstance(resume_id, str) or not resume_id or len(resume_id) > 100):
        raise WorkflowError('继续任务标识无效，请重新打开运行检查')
    project = crud.get_project(uid)
    with Session(get_engine()) as session:
        rows = select(WorkflowRun).where(WorkflowRun.project_uid == uid)
        if resume_id:
            row = session.get(WorkflowRun, resume_id)
            if not row or row.project_uid != uid:
                raise WorkflowError('任务不属于当前项目')
            if row.status not in {'paused', 'failed', 'attention'}:
                issues.append('当前流程不能继续，请刷新流程状态')
            if row.publication_id:
                issues.append('制作已完成，请到发布页继续现有稿件，不会重新制作或重复投稿')
        if session.exec(rows.where(WorkflowRun.status.in_(OPEN))).first():
            issues.append('已有自动流程正在执行或等待审核，请查看当前进度，不要重复启动')
        if not resume_id and mode != 'produce' and session.exec(rows.where(WorkflowRun.publication_id != '')).first():
            issues.append('已有自动流程发布记录，请到发布页继续，不会创建重复稿件')
    if project and any(s.status in {'queued', 'running'} for s in crud.get_steps(project.id)):
        issues.append('制作步骤正在执行或收尾，请等待完成后再运行完整流程')
    return issues


def _plan(uid, raw, *, online=True):
    from dubforge import configuration
    intent = raw or {}
    result = plan(uid, configuration.run_spec(uid, mode=intent.get('mode')), online=online)
    spec = result['recipe']
    comments = any(v['include_comments'] for v in result['version_specs']) if 'version_specs' in result else any(dimensions(v)['include_comments'] for v in spec['variants'])
    if comments and spec['comments_mode'] == 'auto':
        result['steps'].insert(result['steps'].index('assemble'), 'comments')
        result['labels']['comments'] = '采集、精选并翻译上屏评论'
    return result


def preview(uid, raw):
    result = _plan(uid, raw)
    result['blockers'] = list(dict.fromkeys(result['blockers'] + _availability(
        uid, result['recipe']['mode'], (raw or {}).get('resume_id'))))
    return result


def start(uid, body):
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
        project = crud.get_project(uid)
        if any(s.status in {'queued', 'running'} for s in crud.get_steps(project.id)):
            raise WorkflowError('项目已有制作任务，请等待完成')
        video_info.check_idle(uid)
        dubbing_session.check_idle(uid)
        # 确认后到入队之间的本地修改仍必须拦截。
        if _plan(uid, candidate['recipe'], online=False)['fingerprint'] != candidate['fingerprint']:
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
            if candidate['recipe']['mode'] != 'produce':
                previous = session.exec(select(WorkflowRun).where(WorkflowRun.project_uid == uid,
                    WorkflowRun.publication_id != '')).first()
                if previous:
                    raise WorkflowError('本项目已有全流程发布草稿或回执，请到发布页继续该记录；不会创建重复稿件')
            row = WorkflowRun(project_uid=uid, request_id=request, recipe=_json(candidate['recipe']), plan=_json(candidate))
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
    _pool.submit(_execute, key, event)


def _check_cancel(event):
    if event.is_set():
        raise WorkflowError('全流程已停止，已完成结果保留；不会自动继续投稿', code='WORKFLOW_STOPPED')


def _check_configuration(run, steps=None):
    # 与计划冻结的逐键快照对比; steps 限定只看剩余步骤消费的键,
    # 与已完成步骤相关的修改不打断流程 (ADR-0075)。旧运行无快照时退回全量指纹。
    from dubforge.workflow_plan import configuration_snapshot
    stored = run['plan'].get('configuration_snapshot')
    if not isinstance(stored, dict) or 'settings' not in stored:
        if configuration_stamp(Dependencies(crud.get_project(run['project_uid'])),
                               version_mode='version_specs' in run['plan']) != run['plan']['configuration']:
            raise WorkflowError('制作设置在运行期间发生变化，已暂停；请重新预检后继续', code='WORKFLOW_CONFIG_CHANGED')
        return
    current = configuration_snapshot(Dependencies(crud.get_project(run['project_uid'])),
                                     version_mode='version_specs' in run['plan'], steps=steps)
    drifted = (stored['source'] != current['source']
               or {k: v for k, v in stored['settings'].items() if k in current['settings']} != current['settings'])
    if drifted:
        raise WorkflowError('制作设置在运行期间发生变化，已暂停；请重新预检后继续', code='WORKFLOW_CONFIG_CHANGED')


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


def _comments(run, event):
    uid, spec = run['project_uid'], run['recipe']
    # 采集/评分/翻译/挑选与手动链的自动准备共用同一段实现 (comments_auto)。
    # 共享一组上屏评论时，还需满足本次各分P冻结的语言设置，避免到合成才发现超长。
    base = spec['comment_settings']
    display_settings = [base, *[
        {**base, 'language_mode': version['values'].get('comments.language_mode', base['language_mode'])}
        for version in run.get('plan', {}).get('version_specs', []) if version['include_comments']]]
    from dubforge import comments_auto
    try:
        warnings, _count = comments_auto.prepare_overlay(
            uid, spec, cancel_check=lambda: _check_cancel(event), extra_settings=display_settings)
    except DubForgeError as exc:
        if exc.code == 'COMMENTS_AUTO_EMPTY':
            raise WorkflowError(exc.message, code='WORKFLOW_COMMENTS_EMPTY')
        raise
    for warning in warnings:
        run['plan']['warnings'].append(warning)
        _update(run['id'], plan=run['plan'])


def _check_dubbing(run):
    specs = run['plan'].get('version_specs')
    needs_dubbing = (any(v['dubbing'] for v in specs) if specs is not None
                     else any(dimensions(v)['dubbing'] for v in run['recipe']['variants']))
    if needs_dubbing:
        Dependencies(crud.get_project(run['project_uid'])).require_dubbing()


def _prepare_publication(run, event):
    _check_dubbing(run)
    uid, spec = run['project_uid'], run['recipe']
    settings = copy.deepcopy(run['plan']['settings'])
    data = service.candidates(uid)
    # 显式项目/模板值优先，视频资料只补空项；AI不替用户决定分区或权限。
    for key, value in data['suggestion'].items():
        if not settings.get(key) and run['plan']['origins'].get(key) == '默认':
            settings[key] = value
    if spec['ai_copy']:
        from dubforge.publishing.copywriting import generate
        generated = generate(crud.get_project(uid), settings, '')['values']
        for key, value in generated.items():
            if run['plan']['origins'].get(key) == '默认':
                settings[key] = value
    _check_cancel(event)
    # 制作已完成: 发布资料使用计划冻结的设置快照, 只校验来源身份, 不再被制作参数修改打断。
    _check_configuration(run, [])
    if spec['cover_source'] == 'platform':
        cover_id = service.platform_cover(uid)['cover_id']
    else:
        cover_id = spec['cover_id']
        if service._file_hash(service._cover_path(cover_id)) != run['plan']['cover_hash']:
            raise WorkflowError('确认后的封面已变化，请重新选择')
    parts = []
    for variant in (spec['version_ids'] if spec.get('version_ids') is not None else spec['variants']):
        if spec.get('version_ids') is not None:
            version = next(v for v in run['plan']['version_specs'] if v['id'] == variant)
            original = not any(version[k] for k in ('dubbing', 'burn_subtitles', 'include_comments'))
            file = next((f for f in data['files'] if f['exists'] and (f['is_source'] if original else f.get('version_id') == variant)), None)
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
    key = 'workflow-' + run['id']
    pub = service.create(uid, key=key)
    pub = service.save(key, dict(revision=pub['revision'], account_id=spec['account_id'], template_id='',
        cover_id=cover_id, overrides=settings, parts=parts))
    _update(run['id'], publication_id=key)
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
    try:
        with _owned(key):
            _check_cancel(event)
            _update(key, status='running', error='')
            if not run['publication_id']:
                from dubforge.runner import runner
                for index, step in enumerate(run['plan']['steps']):
                    if index < run['cursor']:
                        continue
                    _check_cancel(event)
                    _check_configuration(run, run['plan']['steps'][index:])
                    _update(key, phase=run['plan']['labels'][step])
                    if step == 'comments':
                        _comments(run, event)
                    elif step == 'assemble':
                        _check_dubbing(run)
                        state = comment_studio.view(uid)
                        comment_revision = (run['plan']['overlay_revision'] if run['recipe']['comments_mode'] == 'saved' else state['overlay_revision'])
                        if 'version_specs' in run['plan']:
                            from dubforge.pipeline import assembly_versions
                            specs = copy.deepcopy(run['plan']['version_specs'])
                            overlay = comment_studio.for_task(uid, any(v['include_comments'] for v in specs), comment_revision)
                            for v in specs:
                                if v['include_comments']:
                                    v['comment_overlay'] = {**copy.deepcopy(overlay), 'settings': comment_studio.validate_settings({**overlay['settings'],
                                        **{k.removeprefix('comments.'): value for k, value in v['values'].items() if k.startswith('comments.')}})}
                            runner.start_versions(uid, {}, frozen={'versions': specs})
                        else:
                            variants = [v for v in run['recipe']['variants'] if v != 'src']
                            needs_comments = any(dimensions(v)['include_comments'] for v in variants)
                            runner.start_assemble_outputs(uid, outputs=[dimensions(v) for v in variants],
                                                          overlay_revision=comment_revision if needs_comments else None)
                        _wait_step(uid, step, event)
                    else:
                        result = runner.start_step(uid, step)
                        if result['status'] == 'started':
                            _wait_step(uid, step, event)
                    _update(key, cursor=index + 1)
                _check_cancel(event)
                if run['recipe']['mode'] == 'produce':
                    _update(key, status='completed', phase='制作完成')
                    return
                _update(key, phase='准备发布资料')
                pub = _prepare_publication(run, event)
            else:
                pub = service.view(run['publication_id'])
            if run['recipe']['mode'] == 'draft':
                _update(key, status='ready', phase='发布草稿已准备，可前往发布页核对')
                return
            _check_cancel(event)
            if pub['status'] == 'unknown':
                raise WorkflowError('投稿结果待核对，请到发布页关联回执；禁止自动重投')
            if not pub['receipt'].get('bvid'):
                _update(key, phase='上传并投稿')
                preview = service.plan(pub['id'])
                action = 'submit' if pub['status'] == 'uploaded' else 'publish'
                service.enqueue(pub['id'], dict(action=action, confirmed=True, revision=pub['revision'],
                    fingerprint=preview['fingerprint'], acknowledge_ai_notice=True))
                pub = _wait_publication(pub['id'], event)
            if pub['receipt'].get('bvid'):
                _update(key, status='monitoring', phase='已投稿，等待审核与合集核对', next_check=time.time())
            else:
                raise WorkflowError(pub['error'] or '发布未完成，请核对后继续')
    except Exception as exc:
        message = exc.message if isinstance(exc, DubForgeError) else '全流程未完成，请查看对应步骤和发布记录'
        _update(key, status='paused' if event.is_set() else 'failed', error=message)
    finally:
        with _lock:
            _running.pop(key, None)
            if _leases.get(uid) == key:
                _leases.pop(uid, None)


def resume(key, body):
    run = get(key)
    if run['status'] not in {'paused', 'failed', 'attention'} or key in _running:
        raise WorkflowError('当前任务不能继续')
    if body.get('confirmed') is not True:
        raise WorkflowError('继续前需要重新确认流程范围')
    if run['publication_id']:
        pub = service.view(run['publication_id'])
        if pub['status'] == 'unknown':
            raise WorkflowError('请先在发布页核对投稿回执，不会自动重投')
        if pub['receipt'].get('bvid'):
            _update(key, status='monitoring', phase='继续核对平台状态', next_check=time.time(), check_failures=0, error='')
            return get(key)
        # 已绑定发布文件的任务不重新制作；必须到发布页显式确认当前资料后继续。
        raise WorkflowError('制作已完成，请到发布页核对现有草稿或已上传文件并继续，不能新建重复稿件')
    candidate = _plan(run['project_uid'], body.get('recipe'))
    if candidate['blockers'] or body.get('fingerprint') != candidate['fingerprint']:
        raise WorkflowError('预检未通过或已失效，请重新预检')
    from dubforge.runner import runner
    from dubforge.pipeline import dubbing_session
    with service._lock, runner._start_lock, dubbing_session.gate, video_info.gate, _lock:
        if key in _running or get(key)['status'] not in {'paused', 'failed', 'attention'}:
            raise WorkflowError('此流程已经在继续或状态已变化')
        check_idle(run['project_uid'])
        project = crud.get_project(run['project_uid'])
        if any(s.status in {'running', 'queued'} for s in crud.get_steps(project.id)):
            raise WorkflowError('项目仍有步骤运行或收尾，请稍后继续')
        service.check_project_idle(project.uid)
        video_info.check_idle(project.uid)
        dubbing_session.check_idle(project.uid)
        if _plan(project.uid, candidate['recipe'], online=False)['fingerprint'] != candidate['fingerprint']:
            raise WorkflowError('预检期间设置已变化，请重新确认', code='WORKFLOW_CONFLICT')
        cursor = run['cursor'] if candidate['fingerprint'] == run['plan']['fingerprint'] else 0
        if candidate['fingerprint'] == run['plan']['fingerprint']:
            # 已有产物会让新计划缩短；游标必须继续对应最初确认的步骤表。
            if set(candidate['steps']) - set(run['plan']['steps']):
                cursor = 0  # 暂停期间丢失了原本已就绪的依赖，按新计划补齐。
            else:
                candidate['steps'] = run['plan']['steps']
                candidate['labels'] = run['plan']['labels']
                deps = Dependencies(project, assemble_variants=[v for v in candidate['recipe']['variants'] if v != 'src'] or None)
                if 'version_specs' in candidate: deps = deps.with_versions(candidate['version_specs'])
                for index, step in enumerate(candidate['steps'][:cursor]):
                    if step != 'comments' and not deps.fresh(step):
                        cursor = index
                        break
        _update(key, recipe=candidate['recipe'], plan=candidate, cursor=cursor, status='queued', error='')
        runner._clear_chain(project, '恢复全流程')
        _launch(key, project.uid)
    return get(key)


def stop(key):
    with _lock:
        run = get(key)
        if event := _running.get(key):
            event.set()
            _update(key, status='stopping', phase='正在停止；已发送的投稿将等待回执')
        elif run['status'] == 'monitoring':
            _update(key, status='attention', phase='已停止自动查询；不会撤回B站稿件')
    return get(key)


def monitor_once():
    with Session(get_engine()) as session:
        due = [row.id for row in session.exec(select(WorkflowRun).where(
            WorkflowRun.status == 'monitoring', WorkflowRun.next_check <= time.time())).all()]
    for key in due:
        run = get(key)
        if run['status'] != 'monitoring':
            continue
        _update(key, next_check=time.time() + 60)
        try:
            pub = service.view(run['publication_id'])
            if pub['status'] in service.ACTIVE or pub['id'] in service._running:
                continue
            if not pub['receipt'].get('bvid'):
                raise WorkflowError('缺少投稿回执，请前往发布页核对')
            # 仅自动查询；合集重试必须显式确认，避免写请求结果不明时盲重发。
            if pub['status'] == 'collection_pending':
                _update(key, status='attention', phase='视频已投稿，合集待处理', error=pub['error'])
                continue
            service.enqueue(pub['id'], dict(action='check', revision=pub['revision']))
            pub = _wait_publication(pub['id'], None)
            if get(key)['status'] != 'monitoring':
                continue
            if pub['error']:
                raise WorkflowError(pub['error'])
            if pub['status'] in {'published', 'private', 'reviewed'}:
                _update(key, status='completed', phase='已发布' if pub['status'] == 'published' else '审核已通过 · 仅自己可见' if pub['status'] == 'private' else '修改已生效', error='')
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
