"""投稿后按分P交付冻结章节；远端结果不明时只核对，绝不自动重发。"""
import copy
import json
import logging
import threading
import time

from sqlmodel import Session, select

from dubforge.db import get_engine
from dubforge.errors import DubForgeError
from dubforge.publishing import chapters, lifecycle, service as pub, studio
from dubforge.publishing.models import Publication, PublishChange, now

_lock = threading.RLock()
_running = set()
_stopping = threading.Event()
logger = logging.getLogger(__name__)
MAX_PENDING_ATTEMPTS = 6
_TEMPORARY = {'PUBLISH_CHAPTER_RULE_UNAVAILABLE', 'PUBLISH_CHAPTER_RULE_REJECTED',
              'PUBLISH_CHAPTER_REMOTE_CHANGED', 'PUBLISH_CHAPTER_NOT_SENT',
              'PUBLISH_WORKER_TIMEOUT', 'PUBLISH_TRANSPORT', 'PUBLISH_TIMEOUT'}


def _load(key):
    with Session(get_engine()) as session:
        row = pub._get(session, Publication, key)
        return row


def _json(value):
    return pub._json(value)


def _saved(row):
    try:
        value = json.loads(row.chapter_sync)
        if not isinstance(value, dict) or value and value.get('schema') != 1:
            raise ValueError()
        return value
    except (ValueError, TypeError) as exc:
        raise pub.PublishError('章节交付记录无法读取，请核对本地记录', code='PUBLISH_CHAPTER_STATE_INVALID') from exc


def _initial(row):
    options = json.loads(row.snapshot).get('chapter_options') or {}
    enabled = options.get('enabled') is True
    available = bool(options.get('items'))
    return dict(schema=1, status='pending' if enabled and available else 'skipped',
                phase='等待设置视频章节' if enabled and available else '源视频没有章节，无需同步' if enabled else '未启用自动章节',
                error='', error_code='',
                parts=[], attempts=0, next_check=0, fingerprint='')


def state_view(row):
    from dubforge.execution_progress import normalize
    state = _saved(row) or _initial(row)
    status = state.get('status')
    projected = 'paused' if state.get('error_code') == 'PUBLISH_CHAPTER_PAUSED' else 'waiting' if status == 'pending' else status
    source = state.get('execution') or dict(phase='chapters.' + str(status), label=state.get('phase'))
    return {**state, 'execution': normalize(source, state=projected, owner='chapters', run_id=row.id)}


def view(key):
    row = _load(key)
    return {**state_view(row), 'busy': key in _running}


def _save(key, state, **updates):
    state.update(updates, updated_at=now().isoformat())
    if 'execution' not in updates:
        from dubforge.execution_progress import Recorder
        status, label = state.get('status', 'pending'), state.get('phase', '设置视频章节')
        code = 'chapters.' + status
        if status == 'running':
            code += ('.write' if label.startswith('同步分P') else '.prepare' if label.startswith('检查分P')
                     else '.rules' if label.startswith('读取分P') else '.adapted' if label.startswith('章节已整理')
                     else '.titles')
        projected = 'paused' if state.get('error_code') == 'PUBLISH_CHAPTER_PAUSED' else 'waiting' if status == 'pending' else status
        recorder = Recorder(previous=state.get('execution'), owner='chapters', run_id=key)
        parts = state.get('parts', [])
        state['execution'] = recorder.phase(code, label, detail=state.get('error', ''), state=projected,
            completed=sum(part.get('status') in {'completed', 'skipped'} for part in parts),
            total=len(parts) or None, unit='分P', attempt=state.get('attempts', 0) + 1,
            max_attempts=MAX_PENDING_ATTEMPTS, next_check_at=state.get('next_check') or None)
    with pub._lock, Session(get_engine()) as session:
        row = pub._get(session, Publication, key)
        row.chapter_sync = _json(state)
        session.add(row)
        session.commit()
    return state


def _check_stop(key=None):
    if _stopping.is_set():
        raise pub.PublishError('程序正在停止，已完成章节保留；请重新确认后继续', code='PUBLISH_CHAPTER_STOPPED')
    if key:
        from dubforge import workflows
        if workflows.post_processing_paused(key):
            raise pub.PublishError('已停止发布后处理，已完成章节保留；明确继续后再处理剩余分P',
                                   code='PUBLISH_CHAPTER_PAUSED')


def _stream_cancel(key):
    """流式响应高频检查只定期读持久门禁；每次发送前仍另做立即检查。"""
    checked = [float('-inf')]
    def check():
        _check_stop()
        stamp = time.monotonic()
        if stamp - checked[0] >= .25:
            _check_stop(key)
            checked[0] = stamp
    return check


def _change(key):
    if not key:
        return None
    with Session(get_engine()) as session:
        return session.get(PublishChange, key)


def _pending(key, state, message, code):
    attempts = int(state.get('attempts', 0)) + 1
    terminal = attempts >= MAX_PENDING_ATTEMPTS
    return _save(key, state, status='failed' if terminal else 'pending', attempts=attempts,
        phase='章节仍未就绪，请稍后重试' if terminal else '等待B站章节接口就绪',
        error=message, error_code=code, next_check=0 if terminal else time.time() + 120)


def _cid(value):
    if type(value) is int or isinstance(value, str) and value.isdigit():
        value = int(value)
        return value if 0 < value < 2**53 else None
    return None


def _bind(snapshot, receipt, transfers):
    """上传cid优先；旧数据只接受数量、标题、顺序全都无歧义的回执。"""
    source = snapshot.get('parts')
    remote = receipt.get('videos')
    if not isinstance(source, list) or not source:
        raise pub.PublishError('投稿快照没有分P清单', code='PUBLISH_CHAPTER_INVALID')
    if not isinstance(remote, list) or not remote:
        return None
    remote_ids = [_cid(row.get('cid')) for row in remote if isinstance(row, dict)]
    if len(remote_ids) != len(remote) or None in remote_ids or len(set(remote_ids)) != len(remote_ids):
        raise pub.PublishError('B站分P回执不完整或编号重复，未设置章节', code='PUBLISH_CHAPTER_BINDING_INVALID')
    positional = (len(source) == len(remote)
                  and len({row.get('title') for row in source}) == len(source)
                  and all(left.get('title') and left.get('title') == right.get('title') for left, right in zip(source, remote)))
    result = []
    for part, target in zip(source, remote if positional else [None] * len(source)):
        uploaded = transfers.get(str(part.get('artifact_id')), {})
        identifier = _cid(uploaded.get('cid'))
        if identifier is None and positional:
            identifier = _cid(target.get('cid'))
        if identifier is None:
            raise pub.PublishError('无法把上传成片与B站分P可靠对应，请先核对分P回执', code='PUBLISH_CHAPTER_BINDING_INVALID')
        if identifier not in remote_ids:
            return None
        result.append({**copy.deepcopy(part), 'cid': identifier})
    if len({row['cid'] for row in result}) != len(result):
        raise pub.PublishError('多个成片对应同一B站分P，未设置章节', code='PUBLISH_CHAPTER_BINDING_INVALID')
    return result


def _source_indices(part, source):
    """只核对实际成片的冻结映射，不能拿当前项目方案或源时间补缺。"""
    items, timeline = part.get('chapter_items'), part.get('chapter_timeline') or {}
    if not isinstance(items, list) or timeline.get('kind') not in {'source', 'cut'}:
        raise pub.PublishError('成片缺少已核验的章节时间轴，请先核对该发布记录', code='PUBLISH_CHAPTER_TIMELINE_INVALID')
    if timeline['kind'] == 'cut' and not part.get('output_cuts'):
        raise pub.PublishError('剪辑成片缺少冻结剪辑记录，不能套用源时间轴', code='PUBLISH_CHAPTER_TIMELINE_INVALID')
    indexed = [{**row, '_source_index': index} for index, row in enumerate(source)]
    expected, _ = chapters.map_items(part.get('output_cuts'), indexed)
    if len(expected) != len(items):
        raise pub.PublishError('成片章节与冻结来源不一致，未同步', code='PUBLISH_CHAPTER_TIMELINE_INVALID')
    for frozen, mapped in zip(items, expected):
        if frozen.get('title') != mapped.get('title') or any(frozen.get(path) != mapped.get(path)
                for path in ('start_time', 'end_time')):
            raise pub.PublishError('成片章节时间与冻结来源不一致，未同步', code='PUBLISH_CHAPTER_TIMELINE_INVALID')
    return [row['_source_index'] for row in expected]


def _items(part):
    result = []
    rows = part['chapter_items']
    for index, row in enumerate(rows):
        end = row.get('end_time')
        if end is None:
            end = rows[index + 1]['start_time'] if index + 1 < len(rows) else part.get('duration')
        result.append(dict(title=row['title'], start_time=row['start_time'], end_time=end))
    return result


def _adapt(key, state, account, bvid, aid, part, target, options, indices, *, project_uid='', context_title=''):
    """每P先读规则再整理，同一实际时间轴复用同版本的合格方案。"""
    _save(key, state, phase=f'读取分P章节规则：{part["title"]}')
    rule_view = studio.read(account, 'chapter_rules', dict(bvid=bvid, aid=aid, cid=part['cid']))
    _check_stop(key)
    if (not isinstance(rule_view, dict) or rule_view.get('bvid') != bvid or rule_view.get('aid') != aid
            or rule_view.get('cid') != part['cid'] or not isinstance(rule_view.get('rules'), dict)):
        raise pub.PublishError('B站章节规则响应与当前分P不一致，未设置章节',
                               code='PUBLISH_CHAPTER_RULE_UNAVAILABLE')
    rules = rule_view['rules']
    limits = chapters.platform_constraints(rules)
    source = _items(target)
    cache_key = pub._hash(dict(version=chapters.ADAPTATION_VERSION, items=source,
                              duration=target.get('duration'), translate=options['translate'], rules=limits,
                              instruction=options['instruction'], context_title=context_title))
    plans = state.setdefault('plans', {})
    cached = plans.get(cache_key)
    if cached:
        try:
            result = chapters.validate_adaptation(cached, source, duration=target.get('duration'), rules=rules,
                                                   instruction=options['instruction'], context_title=context_title)
        except pub.PublishError as exc:
            if exc.code != 'PUBLISH_CHAPTER_ADAPT_CACHE_INVALID':
                raise
            cached = None
    if not cached:
        from dubforge.execution_progress import Recorder
        progress = Recorder(lambda value: _save(key, state, execution=value), previous=state.get('execution'),
                            owner='chapters', run_id=key)
        from dubforge import llm_conversations
        with llm_conversations.context(project_uid=project_uid, operation='chapters', label='自动整理并审查视频章节', owner_id=key):
            result = chapters.adapt(source, duration=target.get('duration'), translate=options['translate'], rules=rules,
                                    instruction=options['instruction'], context_title=context_title,
                                    cancel_check=_stream_cancel(key), progress=progress)
        result = chapters.validate_adaptation(result, source, duration=target.get('duration'), rules=rules,
                                               instruction=options['instruction'], context_title=context_title)
        plans[cache_key] = copy.deepcopy(result)
    part['adaptation'] = copy.deepcopy(result['adaptation'])
    for finding in part['adaptation']['review']['findings']:
        finding['source_indices'] = [indices[index] for index in finding['source_indices']]
    part['items'] = [{**item, 'source_indices': [indices[index] for index in item['source_indices']]}
                     for item in result['items']]
    part['plan_key'] = cache_key
    _save(key, state, phase=f'章节已整理：{part["title"]}（{len(source)} → {len(result["items"])}章）')
    return studio._chapter_rows([{path: item[path] for path in ('title', 'start_time', 'end_time')}
                                for item in result['items']], duration=target.get('duration'), rules=rules)


def _aggregate(key, state):
    parts = state.get('parts') or []
    if any(part['status'] == 'unknown' for part in parts):
        message = next((part.get('error') for part in parts if part['status'] == 'unknown' and part.get('error')),
                       '章节写入结果待核对，请先核对，不能重复提交')
        return _save(key, state, status='unknown', phase='章节写入结果待核对',
                     error=message, error_code='PUBLISH_CHAPTER_UNKNOWN', next_check=0)
    if not parts:
        return _save(key, state, status='failed', phase='章节准备已中断，请确认后重试',
                     error='尚未完成章节准备，未发送章节修改请求',
                     error_code='PUBLISH_CHAPTER_INTERRUPTED', next_check=0)
    done = sum(part['status'] in {'completed', 'skipped'} for part in parts)
    if done == len(parts):
        skipped = all(part['status'] == 'skipped' for part in parts)
        return _save(key, state, status='skipped' if skipped else 'completed',
            phase='所选成片没有保留章节，无需同步' if skipped else f'已同步全部 {len(parts)} 个分P章节',
            error='', error_code='', next_check=0)
    message = next((part.get('error') for part in parts if part['status'] == 'failed' and part.get('error')),
                   '已完成章节保留，其余分P请确认后继续；不会重新上传或投稿')
    return _save(key, state, status='failed', phase='部分分P章节尚未完成，请确认后重试',
                 error=message, error_code='PUBLISH_CHAPTER_INCOMPLETE', next_check=0)


def _reconcile(key, state):
    """核对只发读请求；即使核对成功也不在这一轮发送其它分P。"""
    account = state.get('account_id')
    for part in state.get('parts', []):
        change = _change(part.get('change_id'))
        if change and change.status == 'done':
            part.update(status='completed', error='', error_code='')
        elif change and change.status in {'committing', 'unknown'}:
            try:
                result = studio.reconcile(account, change.id, {})
                if result.get('status') == 'done':
                    part.update(status='completed', error='', error_code='')
                else:
                    part.update(status='unknown', error='B站尚未回读到本次章节，请继续核对，不能重复提交')
            except Exception as exc:
                part.update(status='unknown', error=exc.message if isinstance(exc, DubForgeError) else '章节核对未完成，请稍后重试')
            _save(key, state)
    return _aggregate(key, state)


def run(key, reconcile_only=False):
    """一次独立章节交付；失败需明确重试，结果不明时始终只读核对。"""
    if type(reconcile_only) is not bool:
        raise pub.PublishError('章节核对参数无效', code='PUBLISH_CHAPTER_INVALID')
    with _lock:
        if key in _running:
            return view(key)
        _running.add(key)
    state = None
    try:
        row = _load(key)
        lifecycle.assert_active(row)
        state = _saved(row) or _initial(row)
        if state.get('status') == 'unknown' or reconcile_only:
            return _reconcile(key, state) if state.get('parts') else state
        if state.get('status') in {'completed', 'failed'}:
            return state
        if state.get('next_check', 0) > time.time():
            return state
        _check_stop(key)
        snapshot, receipt, transfers = json.loads(row.snapshot), json.loads(row.receipt), json.loads(row.transfers)
        options = pub._chapter_options(snapshot.get('chapter_options'))
        if not options['enabled'] or not options['items']:
            return _save(key, state, status='skipped', phase='未启用自动章节' if not options['enabled'] else '源视频没有章节，无需同步', error='', next_check=0)
        if row.status not in {'published', 'private', 'reviewed'} and receipt.get('remote_state') != 0:
            return _pending(key, state, '等待稿件审核通过并返回分P回执', 'PUBLISH_CHAPTER_NOT_READY')
        account, bvid, aid = snapshot.get('account_id'), receipt.get('bvid'), _cid(receipt.get('aid'))
        if not isinstance(account, str) or not account or not isinstance(bvid, str) or not aid:
            return _pending(key, state, '稿件回执尚不完整，暂未设置章节', 'PUBLISH_CHAPTER_NOT_READY')
        bound = _bind(snapshot, receipt, transfers)
        if bound is None:
            # 审核通过的首次回执可能尚未带齐分P；不能只反复检查旧回执。
            # 仅本轮补读归属已核验的稿件，不写回整份 receipt，避免覆盖并发核对。
            try:
                detail = studio.read(account, 'detail', {'bvid': bvid})
            except DubForgeError as exc:
                if exc.code in _TEMPORARY:
                    return _pending(key, state, exc.message, exc.code)
                raise
            if (not isinstance(detail, dict) or detail.get('mid') != account
                    or detail.get('bvid') != bvid or _cid(detail.get('aid')) != aid):
                raise pub.PublishError('B站稿件归属或编号与发布回执不一致，未设置章节',
                                       code='PUBLISH_CHAPTER_BINDING_INVALID')
            bound = _bind(snapshot, detail, transfers)
            if bound is None:
                return _pending(key, state, 'B站尚未返回全部已上传分P，暂未设置章节', 'PUBLISH_CHAPTER_NOT_READY')
        indices = [_source_indices(part, options['items']) for part in bound]
        # 新增空审查要求不改变旧稿件的身份摘要，否则旧失败记录无法正常重试。
        identity_options = {key: value for key, value in options.items() if key != 'instruction' or value}
        fingerprint = pub._hash(dict(account=account, bvid=bvid, aid=aid, options=identity_options,
            parts=[{k: part.get(k) for k in ('artifact_id', 'sha256', 'cid', 'chapter_items', 'output_cuts', 'duration')} for part in bound]))
        if state.get('fingerprint') and state['fingerprint'] != fingerprint:
            raise pub.PublishError('章节来源或分P回执已变化，请重新核对后重试', code='PUBLISH_CHAPTER_CONFLICT')
        state.update(fingerprint=fingerprint, account_id=account, bvid=bvid, aid=aid)
        if not state.get('parts'):
            state['parts'] = [dict(artifact_id=part['artifact_id'], cid=part['cid'], title=part['title'],
                                  status='pending', error='', error_code='', change_id='') for part in bound]
        _save(key, state, status='running', phase='准备B站章节方案', error='', error_code='', next_check=0)
        for part, target, source_indices in zip(state['parts'], bound, indices):
            if part['status'] in {'completed', 'skipped'}:
                continue
            _check_stop(key)
            lifecycle.assert_active(_load(key))
            if not target['chapter_items']:
                part.update(status='skipped', error='所含源章节已全部剪去')
                _save(key, state)
                continue
            change = _change(part.get('change_id'))
            if change and change.status == 'done':
                part.update(status='completed', error='', error_code='')
                _save(key, state)
                continue
            if change and change.status in {'committing', 'unknown'}:
                part.update(status='unknown', error='章节写入结果待核对')
                return _save(key, state, status='unknown', phase='章节写入结果待核对', error=part['error'])
            try:
                # 旧一对一 translation 缓存不具备平台合规证明，不能直接作为新方案。
                items = _adapt(key, state, account, bvid, aid, part, target, options, source_indices,
                               project_uid=row.project_uid, context_title=snapshot.get('settings', {}).get('title', ''))
                if any(finding['action'] == 'needs_review' for finding in part['adaptation']['review']['findings']):
                    raise pub.PublishError('章节标题仍有待人工核对的内容，已保留整理方案与具体原因；请核对标题后手动同步',
                                           code='PUBLISH_CHAPTER_REVIEW_REQUIRED')
                if change and change.status == 'prepared':
                    old = json.loads(change.preview).get('after') or {}
                    if old.get('items') != items or old.get('permanent') is not options['permanent']:
                        change = None
                        part['change_id'] = ''
                if not change or change.status == 'failed':
                    _save(key, state, phase=f'检查分P章节：{part["title"]}')
                    prepared = studio.prepare(account, dict(bvid=bvid, kind='chapters', values=dict(
                        aid=aid, cid=part['cid'], items=items, permanent=options['permanent'])))
                    part['change_id'] = prepared['id']
                    _save(key, state)
                    if prepared['preview']['before']['items']:
                        raise pub.PublishError('B站已有不同章节，自动流程未覆盖，请手动核对', code='PUBLISH_CHAPTER_EXISTING')
                elif change.status == 'prepared' and (json.loads(change.preview).get('before') or {}).get('items'):
                    # 准备后进程中断也不能绕过“保留已有手工章节”的边界。
                    raise pub.PublishError('B站已有不同章节，自动流程未覆盖，请手动核对', code='PUBLISH_CHAPTER_EXISTING')
                _check_stop(key)
                lifecycle.assert_active(_load(key))
                part.update(status='running', error='', error_code='')
                _save(key, state, phase=f'同步分P章节：{part["title"]}')
                result = studio.commit(account, part['change_id'], {'confirmed': True})
                if result.get('status') != 'done':
                    raise pub.PublishError('章节写入结果尚未确认', code='PUBLISH_CHAPTER_UNKNOWN')
                part.update(status='completed', error='', error_code='')
                _save(key, state)
            except Exception as exc:
                code = getattr(exc, 'code', '')
                if code == 'PUBLISH_CHAPTER_NOOP':
                    part.update(status='completed', error='', error_code='')
                    _save(key, state)
                    continue
                message = exc.message if isinstance(exc, DubForgeError) else '章节处理未完成，请核对结果'
                change = _change(part.get('change_id'))
                unknown = code == 'PUBLISH_CHAPTER_UNKNOWN' or change and change.status in {'committing', 'unknown'}
                part.update(status='unknown' if unknown else 'failed', error=message,
                            error_code=code or 'PUBLISH_CHAPTER_FAILED')
                if code == 'PUBLISH_CHAPTER_PAUSED' and not unknown:
                    return _save(key, state, status='pending', phase='已停止发布后处理',
                                 error=message, error_code=code, next_check=0)
                if code in _TEMPORARY and not unknown:
                    part['change_id'] = ''
                    return _pending(key, state, message, code)
                return _save(key, state, status='unknown' if unknown else 'failed',
                    phase='章节写入结果待核对' if unknown else '章节设置未完成', error=message, error_code=code, next_check=0)
        return _aggregate(key, state)
    except Exception as exc:
        if state is None:
            raise
        message = exc.message if isinstance(exc, DubForgeError) else '章节处理未完成，请稍后核对'
        paused = getattr(exc, 'code', '') == 'PUBLISH_CHAPTER_PAUSED'
        return _save(key, state, status='pending' if paused else 'failed',
                     phase='已停止发布后处理' if paused else '章节设置未完成', error=message,
                     error_code=getattr(exc, 'code', 'PUBLISH_CHAPTER_FAILED'), next_check=0)
    finally:
        with _lock:
            _running.discard(key)


def reset_for_retry(key, enable=False):
    """仅显式重试使用；不能清除尚未核对的远端写入保护。"""
    if type(enable) is not bool:
        raise pub.PublishError('章节启用参数无效', code='PUBLISH_CHAPTER_INVALID')
    with pub._lock, _lock, Session(get_engine()) as session:
        if key in _running:
            raise pub.PublishError('章节正在处理，请稍后重试', code='PUBLISH_CHAPTER_BUSY')
        row = pub._get(session, Publication, key)
        lifecycle.assert_active(row)
        options = pub._chapter_options(json.loads(row.snapshot).get('chapter_options'))
        if not options['enabled'] and not enable:
            raise pub.PublishError('此发布记录尚未开启自动章节，请明确选择启用后再同步', code='PUBLISH_CHAPTER_DISABLED')
        state = _saved(row) or _initial(row)
        for part in state.get('parts', []):
            change = _change(part.get('change_id'))
            if change and change.status == 'done':
                # 写后回执已落库，不能因章节摘要落库中断而丢掉成功证据。
                part.update(status='completed', error='', error_code='')
                continue
            if part['status'] == 'unknown' or change and change.status in {'committing', 'unknown'}:
                raise pub.PublishError('章节写入结果待核对，请先只读核对，不能重新提交', code='PUBLISH_CHAPTER_UNKNOWN')
            if part['status'] not in {'completed', 'skipped'}:
                part.update(status='pending', error='', error_code='', change_id='')
        if enable:
            snapshot, draft = json.loads(row.snapshot), json.loads(row.draft)
            options = pub._chapter_options({**(snapshot.get('chapter_options') or {}), 'enabled': True})
            snapshot['chapter_options'], draft['chapter_options'] = options, copy.deepcopy(options)
            row.snapshot, row.draft, row.revision = _json(snapshot), _json(draft), row.revision + 1
        state.update(schema=1, status='pending', phase='已确认重试章节', error='', error_code='', attempts=0, next_check=0)
        from dubforge.execution_progress import Recorder
        state['execution'] = Recorder(previous=state.get('execution'), restart=True, owner='chapters',
            run_id=key).phase('chapters.queued', '等待继续视频章节', state='queued',
                detail='已受理，正在准备未完成章节；已完成的分P将复用')
        row.chapter_sync = _json(state)
        session.add(row)
        session.commit()
    return view(key)


def recover():
    """启动时只检查本地回执，不在恢复阶段请求远端或重新发送。"""
    with Session(get_engine()) as session:
        keys = []
        for row in session.exec(select(Publication)).all():
            try:
                if _saved(row).get('status') == 'running':
                    keys.append(row.id)
            except pub.PublishError:
                # 单条损坏记录应当保留取证，不能阻断其它记录或应用启动。
                logger.warning('章节交付记录无法恢复：%s', row.id)
    for key in keys:
        if key in _running:
            continue
        state = _saved(_load(key))
        for part in state.get('parts', []):
            change = _change(part.get('change_id'))
            if change and change.status == 'done':
                part.update(status='completed', error='', error_code='')
            elif change and change.status in {'committing', 'unknown'}:
                part.update(status='unknown', error='程序重启，章节写入结果需核对')
            elif part['status'] not in {'completed', 'skipped'}:
                part.update(status='failed', error='程序重启，章节处理中断；确认后可继续')
        _aggregate(key, state)


def startup():
    _stopping.clear()


def shutdown():
    _stopping.set()
