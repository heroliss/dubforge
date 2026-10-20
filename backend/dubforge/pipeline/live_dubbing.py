"""运行中的逐句生成与精调：持久保存意图，在当前请求结束后合并执行。

所有写入均持 Runner 启动锁，参数和队列在同一次 Project 更新中提交。
队列不创建后台任务，只有已经持有本次运行资格的 TTS 执行器可以消费。
"""
import copy
import datetime
import json
import hashlib
import shutil
import uuid
from contextlib import contextmanager

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_edit, line_settings

KEY = '_tts_live_updates'
REVISIONS = '_tts_line_revisions'
PENDING = {'queued', 'running'}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def _runner():
    from dubforge.runner import runner
    return runner


def _control(project):
    value = crud.project_params(project).get(KEY, {})
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _write(project, params, control):
    params[KEY] = control
    project.params = json.dumps(params, ensure_ascii=False)
    crud.save_project(project)


def _active(project):
    runner = _runner()
    with runner._lock:
        key = (project.id, 'tts')
        job = runner._current_jobs.get(key)
        cancel = runner._cancels.get(key)
        abandoned = key in runner._abandoned
    step = crud.get_step(project.id, 'tts')
    active = bool(job and job.status == 'running' and step and step.status in {'queued', 'running'})
    stopping = bool(active and (abandoned or cancel is not None and cancel.is_set()))
    return job, active, stopping


def _accepting(project, job, control):
    """空队列只需当前执行器门闸，不能为每次普通制作改写项目参数。"""
    runner = _runner()
    with runner._lock:
        ctx = runner._ctxs.get((project.id, 'tts'))
        closed = ctx is not None and ctx.job_id == job.id and not getattr(ctx, '_line_updates_accepting', True)
    return not closed and (control.get('job_id') != job.id or control.get('accepting', True))


def _generation_accepting(project, job, control):
    runner = _runner()
    with runner._lock:
        ctx = runner._ctxs.get((project.id, 'tts'))
        ready = ctx is not None and ctx.job_id == job.id and getattr(ctx, '_tts_queue_ready', False)
    return ready and _accepting(project, job, control)


def _task(project, job):
    """主执行范围只读；追加意图另存，避免请求线程改写执行线程的 task_data。"""
    runner = _runner()
    with runner._lock:
        ctx = runner._ctxs.get((project.id, 'tts'))
        if ctx is not None and ctx.job_id == job.id:
            return ctx, copy.deepcopy(ctx.task_data)
    return None, json.loads(job.task_data)


def reserved_takes_by_line(project):
    """只为仍在等待或生成的句子预留槽号；别句预留不占用本句空位。"""
    job, active, _ = _active(project)
    if not active:
        return {}
    _, task = _task(project, job)
    slots = {}

    def reserve(ident, take):
        if type(ident) is int and ident >= 1 and type(take) is int and 1 <= take <= 10000:
            slots.setdefault(ident, set()).add(take)

    for row in task.get('lines', []):
        if row.get('status') in PENDING:
            reserve(row.get('id'), row.get('take'))
    control = _control(project)
    if control.get('job_id') == job.id:
        for ident, row in control.get('lines', {}).items():
            if row.get('state') in PENDING and isinstance(ident, str) and ident.isdecimal():
                reserve(int(ident), row.get('target_take'))
    return slots


def reserved_takes(project, line_ids=None):
    """槽目录可读取全部预留，单句或勾选生成只读取其自身预留。"""
    selected = set(line_ids) if line_ids is not None else None
    return {take for ident, takes in reserved_takes_by_line(project).items()
            if selected is None or ident in selected for take in takes}


def enqueue_generation(project, line_ids, *, takes=None, force=True, expected=None):
    """显式生成附着当前任务；不修改设置、不另开制作链或并发模型请求。"""
    from dubforge.pipeline import dubbing_task
    runner = _runner()
    with runner._start_lock:
        project = crud.get_project(project.uid)
        job, active, stopping = _assert_writable(project)
        control = _control(project)
        if not active or stopping or not _generation_accepting(project, job, control):
            raise DubForgeError('本轮尚未开始接收句子，或已进入停止、最终排布阶段；请稍后再生成',
                                code='LINE_UPDATE_QUEUE_CLOSED')
        ctx, task = _task(project, job)
        deps = ctx.dependencies()
        deps.project = project
        prepared = dubbing_task.prepare(project, deps, line_ids, takes, expected,
                                       allow_unrelated_slot_changes=True)
        lines = {line['id']: line for line in deps.dubbing_lines(required=True)}
        params = crud.project_params(project)
        if control.get('job_id') != job.id:
            control = dict(job_id=job.id, accepting=True, lines={})
        rows, receipt = control.setdefault('lines', {}), []
        planned = {row['id']: row for row in task.get('lines', [])}
        request_id = uuid.uuid4().hex
        for target in prepared['lines']:
            ident, take = str(target['id']), target['take']
            line = lines[target['id']]
            values = line_settings.overrides(project, line)
            revision = params.get(REVISIONS, {}).get(ident, 0)
            existing = rows.get(ident, {})
            initial = planned.get(target['id'], {})
            pending = existing.get('state') in PENDING
            initial_pending = initial.get('status') in PENDING and not initial.get('live_updated')
            occupied = existing.get('target_take', existing.get('running_take')) if pending else None
            if occupied is None and pending:
                selection = dubbing_edit.selection(project, line, deps.root, dependencies=deps)
                occupied = selection.get('take') if selection.get('source') == 'dub' else None
                if occupied is None:
                    occupied = max(dubbing_edit.take_ids(deps.root, line['id']), default=0) + 1
            if occupied is None and initial_pending:
                occupied = initial.get('take')
            if occupied is not None and occupied != take:
                raise DubForgeError(f'第 {line["id"]} 句已在配音槽 {occupied} 等待或生成；'
                                    f'不能同时改为配音槽 {take}，请完成后再生成到其他槽',
                                    code='TTS_QUEUE_TARGET_CONFLICT')
            duplicate = (pending and existing.get('settings') == values
                         and (not force or existing.get('force', True)))
            if initial_pending and not pending and initial.get('kind') != 'repair':
                frozen = ctx._tts_line_settings_snapshot.get(ident, {})
                frozen_values = frozen.get('settings', {}) if frozen.get('identity') == line_settings.identity(line) else {}
                if task.get('policy', {}).get('line_settings') == 'project':
                    from dubforge.pipeline.pronunciation import preserve_records
                    frozen_values = preserve_records({ident: frozen}).get(ident, {}).get('settings', {})
                initial_force = bool(initial.get('force')) if task.get('scope') == 'policy' else bool(ctx.force)
                will_generate = initial_force or not dubbing_edit.playable(project, line, deps.root, take, dependencies=deps)
                duplicate = frozen_values == values and (not force or will_generate)
            if duplicate:
                receipt.append({**target, 'status': existing.get('state') if pending else initial['status'],
                                'already_queued': True})
                continue
            # 同句正在精调时只能合并后续设置，不能改写正在执行的候选快照。
            running = existing.get('state') == 'running'
            row = existing if existing else {}
            row.update(kind='generation', identity=line_settings.identity(line), settings=copy.deepcopy(values),
                       target_take=take, force=bool(force), request_id=request_id,
                       requested_revision=revision, pending_revision=revision,
                       state='running' if running else 'queued', error=None, cached=False, requested_at=_now(),
                       message=f'已加入待配音，当前请求结束后生成到配音槽 {take}')
            rows[ident] = row
            receipt.append({**target, 'request_id': request_id})
        control.setdefault('accepted_request_ids', []).append(request_id)
        _write(project, params, control)
        # 清单只是诊断副本，已入队不能因为副本失败被界面误认为失败而再次提交。
        try:
            crud.write_manifest(project.uid)
        except OSError:
            pass
        return dict(status='queued', job_id=job.id, queue_id=request_id, request_id=request_id,
                    lines=receipt)


def view(project, line_id=None):
    """读取磁盘意图并核对真实任务，进程重启后不把旧队列伪装成等待执行。"""
    control = _control(project)
    job, active, stopping = _active(project)
    owns_control = bool(active and control.get('job_id') == job.id)
    accepting = bool(active and not stopping and _accepting(project, job, control))
    params = crud.project_params(project)
    latest = job or crud.latest_job(project.id, 'tts')
    # 新一轮普通合成无需改写旧控制记录，也不能继续显示旧轮的精调失败标签。
    show_control = control.get('job_id') is None or latest is None or control.get('job_id') == latest.id
    entries = control.get('lines', {}) if show_control else {}
    rows = {}
    for ident, raw in entries.items():
        row = {k: copy.deepcopy(v) for k, v in raw.items() if k not in {'settings', 'identity'}}
        if row.get('state') in PENDING and not owns_control:
            row.update(state='interrupted', pending_revision=None, running_revision=None,
                       message='原任务已结束；设置已保留，请手动继续或重新生成')
        elif row.get('state') in PENDING and stopping:
            row.update(message='已停止接收更新，待执行修改不会继续合成')
        rows[ident] = row
    editable = project.archived_at is None
    generation_accepting = bool(accepting and _generation_accepting(project, job, control))
    reason = '项目已归档，请先恢复项目' if not editable else ''
    summary = dict(editable=editable, active=active, accepting=accepting and editable,
                   reason=reason,
                   generation_accepting=generation_accepting and editable,
                   generation_reason=reason or (
                       '本轮已停止接收新句子，正在停止或最终排布' if active and not accepting else
                       '正在准备本轮配音范围，请开始合成后再加入待配音' if active and not generation_accepting else ''),
                   job_id=job.id if active else control.get('job_id'),
                   pending=sum(r.get('state') == 'queued' or r.get('pending_revision') is not None for r in rows.values()),
                   running=sum(r.get('state') == 'running' for r in rows.values()),
                   failed=sum(r.get('state') == 'failed' for r in rows.values()), lines=rows)
    if line_id is None:
        return summary
    return line_view(summary, line_id, params.get(REVISIONS, {}).get(str(line_id), 0))


def line_view(summary, line_id, revision=0):
    """列表共用一次运行状态查询，不逐句访问数据库。"""
    row = summary['lines'].get(str(line_id), {})
    message = row.get('message')
    requested = row.get('requested_revision')
    if requested is not None and revision != requested:
        message = (f'设置 r{revision} 已仅保存供下次使用；本轮仍执行已排队的 r{requested}'
                   if row.get('state') in PENDING else
                   f'设置 r{revision} 已保存但尚未应用；当前声音使用 r{row.get("completed_revision")}'
                   if row.get('state') == 'done' else
                   f'设置 r{revision} 已仅保存；之前 r{requested} 的更新'
                   + ('失败' if row.get('state') == 'failed' else '尚未完成') + '，旧配音保留')
    return {**{k: v for k, v in summary.items() if k not in {'lines', 'pending', 'running', 'failed'}},
            'state': 'saved', 'revision': revision, **row,
            'message': message or ('保存后将在当前句结束时更新当前配音槽' if summary['accepting']
                        else '仅保存设置；再次生成时使用')}


def _assert_writable(project):
    from dubforge import runtime_edit
    from dubforge.pipeline import dubbing_session
    # 单句精调只改变未来请求，不能借此编辑文字、时间轴和现有声音选择。
    runtime_edit.assert_editable('project:' + project.uid, paths={'tts_lines.live'})
    job, active, stopping = _active(project)
    if project.uid in dubbing_session._active and not active:
        raise DubForgeError('当前单句合成不支持运行队列，请等待当前句完成', code='PIPELINE_BUSY')
    return job, active, stopping


def save(uid, line, updates, reset, *, execution='auto', revision=None):
    """显式逐句保存；auto 只附着既有任务，绝不因保存而另起制作。"""
    if execution not in {'auto', 'save', 'retry'}:
        raise DubForgeError('请选择保存并更新配音，或仅保存设置', code='LINE_UPDATE_INVALID')
    if execution == 'retry' and (updates or reset):
        raise DubForgeError('重试仅使用已保存的本句设置，请先保存新的修改', code='LINE_UPDATE_INVALID')
    runner = _runner()
    with runner._start_lock:
        project = crud.get_project(uid)
        job, active, stopping = _assert_writable(project)
        params = crud.project_params(project)
        current_revision = params.get(REVISIONS, {}).get(str(line['id']), 0)
        if revision is not None and (type(revision) is not int or revision != current_revision):
            raise DubForgeError('本句设置已在其他窗口修改，请刷新后再保存', code='LINE_SETTINGS_CONFLICT')
        control = _control(project)
        if active and execution in {'auto', 'retry'} and (stopping or not _accepting(project, job, control)):
            raise DubForgeError('本轮配音已停止接收更新；可以仅保存设置供下次生成使用',
                                code='LINE_UPDATE_QUEUE_CLOSED')
        if execution == 'retry' and (not active or control.get('job_id') != job.id
                or control.get('lines', {}).get(str(line['id']), {}).get('state') not in {'failed', 'interrupted'}):
            raise DubForgeError('本轮没有可重试的本句更新；任务结束后请使用生成配音', code='LINE_UPDATE_RETRY_UNAVAILABLE')
        values = line_settings.updated_values(project, line, updates, reset)
        changed = values != line_settings.overrides(project, line)
        recorded_revision = (control.get('lines', {}).get(str(line['id']), {}).get('requested_revision')
                             if job and control.get('job_id') == job.id else None)
        apply_saved = bool(active and execution in {'auto', 'retry'} and (current_revision or execution == 'retry')
                           and (recorded_revision != current_revision or execution == 'retry'))
        if not changed and not apply_saved:
            return dict(ok=True, overridden=list(values), revision=current_revision,
                        execution=view(project, line['id']))
        current_revision += int(changed)
        params.setdefault(REVISIONS, {})[str(line['id'])] = current_revision
        if values:
            params.setdefault('tts_lines', {})[str(line['id'])] = dict(identity=line_settings.identity(line), settings=values)
        else:
            params.setdefault('tts_lines', {}).pop(str(line['id']), None)
        if active and execution in {'auto', 'retry'}:
            if control.get('job_id') != job.id:
                control = dict(job_id=job.id, accepting=True, lines={})
            row = control.setdefault('lines', {}).setdefault(str(line['id']), {})
            running = row.get('state') == 'running'
            row.update(identity=line_settings.identity(line), settings=copy.deepcopy(values),
                       requested_revision=current_revision, pending_revision=current_revision,
                       state='running' if running else 'queued', error=None, requested_at=_now(),
                       message='当前请求结束后使用最新设置更新当前配音槽')
        else:
            # 仅保存不撤销用户已经明确排队的旧请求，其 requested_revision 会单独显示。
            if not active:
                control = dict(job_id=None, accepting=False, lines={})
                control['lines'][str(line['id'])] = dict(state='saved', requested_revision=None,
                    completed_revision=None, message='设置已保存；再次生成时使用')
        _write(project, params, control)
        crud.write_manifest(uid)
        return dict(ok=True, overridden=list(values), revision=current_revision,
                    execution=view(project, line['id']))


def begin(ctx):
    with _runner()._start_lock:
        ctx._line_updates_accepting = True
        ctx._tts_queue_ready = False


def ready(ctx):
    """只在范围与原始逐句快照已就绪后开放追加，排队中的策略仍保持可修改。"""
    with _runner()._start_lock:
        ctx._tts_queue_ready = True


def claim(ctx, lines):
    """单次领取最新版本；重复保存合并，同一句运行中再改只追加最新一版。"""
    with _runner()._start_lock:
        ctx.check_cancel()
        project = crud.get_project(ctx.project.uid)
        params, control = crud.project_params(project), _control(project)
        if control.get('job_id') != ctx.job_id or not control.get('accepting'):
            return None
        for ident, row in control.get('lines', {}).items():
            revision = row.get('pending_revision')
            if revision is None or row.get('state') == 'running':
                continue
            line = lines.get(int(ident))
            if line is None or row.get('identity') != line_settings.identity(line):
                row.update(state='failed', pending_revision=None, error='句子文字或时间已变化，未执行旧精调')
                _write(project, params, control)
                continue
            try:
                selected = dubbing_edit.selection(project, line, ctx.pdir)
                # 显式生成固定所确认的目标；其后的精调继续沿用该目标。
                take = row.get('target_take')
                if take is None:
                    take = selected.get('take') if selected.get('source') == 'dub' else None
                if take is None:
                    take = max(dubbing_edit.take_ids(ctx.pdir, line['id']), default=0) + 1
                dubbing_edit.take_name(line['id'], take)
            except (DubForgeError, OSError) as exc:
                row.update(state='failed', pending_revision=None,
                           error='无法确定本句配音槽，请检查选用声音后重试', message='更新失败，之前的配音未改动')
                _write(project, params, control)
                continue
            settings = copy.deepcopy(row.get('settings', {}))
            row.update(state='running', running_revision=revision, pending_revision=None,
                       running_take=take, started_at=_now(),
                       message=f'正在生成到配音槽 {take}' if row.get('kind') == 'generation'
                               else f'正在用已固定的本句设置更新配音槽 {take}')
            _write(project, params, control)
            request = copy.deepcopy(row)
            request.update(id=line['id'], revision=revision, take=take, settings=settings)
            return request
    return None


def _finish_row(row, request, error=None):
    pending = row.get('pending_revision') is not None
    row.update(running_revision=None, running_take=None, finished_at=_now(),
               state='queued' if pending else 'failed' if error else 'done',
               error=error, completed_revision=request['revision'] if not error else row.get('completed_revision'),
               take=request['take'] if not error else row.get('take'),
               message='等待应用更新后的设置' if pending else
                       '生成失败，继续保留之前选用的配音' if error else
                       '配音已生成，请重新试听' if row.get('kind') == 'generation' else '当前配音槽已更新，请重新试听')


def candidate_path(ctx, line):
    """候选声音不占用户配音槽，也不进入正式产物索引。"""
    return ctx.pdir / 'artifacts' / 'tts' / f'.live-{line["id"]}-{uuid.uuid4().hex}.wav'


def discard_candidate(path):
    for suffix in ('.wav', '.input', '.take.json'):
        try:
            path.with_suffix(suffix).unlink(missing_ok=True)
        except OSError:
            pass  # 临时文件清理不能把已提交的声音更新误报失败。


@contextmanager
def _publish_candidate(candidate, target):
    """只在发布与数据库提交期间保留回滚副本，成功后立即清理。"""
    from dubforge.utils.fs import atomic_replace
    backups, published, recovered = {}, [], False
    try:
        for suffix in ('.wav', '.input', '.take.json'):
            destination = target.with_suffix(suffix)
            backup = destination.with_name('.' + destination.name + '.rollback-' + uuid.uuid4().hex)
            if destination.is_file():
                # copy2 保留纳秒时间戳，回滚后原满意标记及后期音量绑定仍有效。
                shutil.copy2(destination, backup)
                backups[destination] = backup
            else:
                backups[destination] = None
        for suffix in ('.wav', '.input', '.take.json'):
            destination = target.with_suffix(suffix)
            atomic_replace(candidate.with_suffix(suffix), destination)
            published.append(destination)
        yield
        recovered = True
    except BaseException:
        for destination in reversed(published):
            backup = backups[destination]
            if backup is None:
                destination.unlink(missing_ok=True)
            else:
                atomic_replace(backup, destination)
        recovered = True
        raise
    finally:
        # 回滚本身失败时保留副本供人工恢复，不能再销毁唯一旧声音。
        if recovered or not published:
            for backup in backups.values():
                if backup is not None:
                    try:
                        backup.unlink(missing_ok=True)
                    except OSError:
                        pass


def complete(ctx, request, line, *, candidate=None):
    """声音及元数据就绪后，选择、队列成功与任务回执在同一事务提交。"""
    from sqlalchemy import update
    from dubforge.domain.models import Project, Job, Artifact
    if candidate is None:
        raise DubForgeError('本句缺少完整候选声音，未替换当前配音', code='DUB_TAKE_MISSING')
    snapshot = json.loads(candidate.with_suffix('.take.json').read_text(encoding='utf-8'))
    if (snapshot.get('identity') != line_settings.identity(line)
            or snapshot.get('version') != dubbing_edit.file_version(candidate)
            or snapshot.get('fingerprint') != candidate.with_suffix('.input').read_text(encoding='utf-8')):
        raise DubForgeError('候选配音的文字或声音快照不匹配，未替换当前配音', code='DUB_TAKE_MISSING')
    with candidate.open('rb') as stream:
        sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
    size = candidate.stat().st_size
    target = dubbing_edit.take_path(ctx.pdir, line['id'], request['take'])
    with _runner()._start_lock:
        ctx.check_cancel()
        project = crud.get_project(ctx.project.uid)
        params, control = crud.project_params(project), _control(project)
        row = control.get('lines', {}).get(str(request['id']))
        if control.get('job_id') != ctx.job_id or not row or row.get('running_revision') != request['revision']:
            raise DubForgeError('本句更新任务已变化，未切换选用配音', code='LINE_UPDATE_CONFLICT')
        params.setdefault('dubbing_edit', {})[str(line['id'])] = dict(
            identity=line_settings.identity(line), source='dub', take=request['take'])
        _finish_row(row, request)
        row['cached'] = False
        params[KEY] = control
        raw_params = json.dumps(params, ensure_ascii=False)
        payload = dict(ctx.task_data)
        if ctx._execution_snapshot is not None:
            payload['execution_snapshot'] = ctx._execution_snapshot
        raw_task = json.dumps(payload, ensure_ascii=False)
        with crud.session() as session:
            saved_job = session.get(Job, ctx.job_id)
            if session.get(Project, project.id) is None or saved_job is None or saved_job.status != 'running':
                raise DubForgeError('制作任务已经结束，未切换选用配音', code='LINE_UPDATE_QUEUE_CLOSED')
            ctx.check_cancel()
            session.exec(update(Project).where(Project.id == project.id).values(
                params=raw_params, updated_at=datetime.datetime.now(datetime.timezone.utc)))
            session.exec(update(Job).where(Job.id == ctx.job_id).values(task_data=raw_task))
            session.add(Artifact(project_id=project.id, step_key='tts', kind='audio',
                name=target.name, rel_path=str(target.relative_to(ctx.pdir)),
                sha256=sha256, size=size, **ctx.artifact_audit()))
            # 回执和产物索引同一事务；提交失败或取消原子恢复旧三文件。
            with _publish_candidate(candidate, target):
                ctx.check_cancel()
                session.commit()
        project.params = raw_params
        ctx.project = project
        ctx._job.task_data = raw_task
        # manifest 是人类可读副本；失败不能把已提交的新选择误报成精调失败。
        try:
            crud.write_manifest(project.uid)
        except Exception:
            try:
                ctx.log('⚠ 配音更新已保存，项目清单副本稍后重建')
            except Exception:
                pass


def complete_cached(ctx, request, line):
    """非强制生成复用已有效声音；不重写文件、不清除满意标记。"""
    from sqlalchemy import update
    from dubforge.domain.models import Project, Job
    with _runner()._start_lock:
        ctx.check_cancel()
        project = crud.get_project(ctx.project.uid)
        params, control = crud.project_params(project), _control(project)
        row = control.get('lines', {}).get(str(request['id']))
        if control.get('job_id') != ctx.job_id or not row or row.get('running_revision') != request['revision']:
            raise DubForgeError('本句生成任务已变化，未切换选用配音', code='LINE_UPDATE_CONFLICT')
        params.setdefault('dubbing_edit', {})[str(line['id'])] = dict(
            identity=line_settings.identity(line), source='dub', take=request['take'])
        _finish_row(row, request)
        row['cached'] = True
        row['message'] = '已复用此槽有效配音' if row.get('state') == 'done' else row['message']
        params[KEY] = control
        raw_params = json.dumps(params, ensure_ascii=False)
        payload = dict(ctx.task_data)
        if ctx._execution_snapshot is not None:
            payload['execution_snapshot'] = ctx._execution_snapshot
        raw_task = json.dumps(payload, ensure_ascii=False)
        with crud.session() as session:
            saved_job = session.get(Job, ctx.job_id)
            if saved_job is None or saved_job.status != 'running':
                raise DubForgeError('制作任务已经结束，未切换选用配音', code='LINE_UPDATE_QUEUE_CLOSED')
            session.exec(update(Project).where(Project.id == project.id).values(params=raw_params,
                updated_at=datetime.datetime.now(datetime.timezone.utc)))
            session.exec(update(Job).where(Job.id == ctx.job_id).values(task_data=raw_task))
            ctx.check_cancel()
            session.commit()
        project.params = raw_params
        ctx.project, ctx._job.task_data = project, raw_task
        try:
            crud.write_manifest(project.uid)
        except OSError:
            pass


def finish(ctx, request, *, error=None):
    with _runner()._start_lock:
        project = crud.get_project(ctx.project.uid)
        params, control = crud.project_params(project), _control(project)
        if control.get('job_id') != ctx.job_id:
            return
        row = control.get('lines', {}).get(str(request['id']))
        if not row or row.get('running_revision') != request['revision']:
            return
        _finish_row(row, request, error)
        _write(project, params, control)


def close_if_empty(ctx):
    """最后一个队列检查与关闭同锁；关闭先到则保存请求必须明确选择仅保存。"""
    with _runner()._start_lock:
        project = crud.get_project(ctx.project.uid)
        params, control = crud.project_params(project), _control(project)
        if control.get('job_id') != ctx.job_id:
            ctx._line_updates_accepting = False
            return True
        if any(row.get('pending_revision') is not None or row.get('state') == 'running'
               for row in control.get('lines', {}).values()):
            return False
        control['accepting'] = False
        ctx._line_updates_accepting = False
        _write(project, params, control)
        return True


def end(ctx):
    """停止/异常后显式终结旧队列，崩溃时 view 也会按真实任务标记中断。"""
    with _runner()._start_lock:
        ctx._line_updates_accepting = False
        project = crud.get_project(ctx.project.uid)
        if project is None:
            return
        params, control = crud.project_params(project), _control(project)
        if control.get('job_id') != ctx.job_id:
            return
        control['accepting'] = False
        for row in control.get('lines', {}).values():
            if row.get('state') in PENDING:
                row.update(state='interrupted', pending_revision=None, running_revision=None,
                           message='本轮已结束；设置与旧配音均保留，请手动继续或重新生成')
        _write(project, params, control)
        # 历史任务也保留本轮更新回执；下一轮清空活动队列不会抹去失败原因。
        ctx.task_data['line_config_updates'] = copy.deepcopy(control)
        ctx.save_task_data()
