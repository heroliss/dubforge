"""混合批量运行：合集组复用顺序发布，独立项目复用原有完整流程。"""
import copy
import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager

from sqlmodel import Session, select
from sqlalchemy.orm import load_only

from dubforge import configuration, release_batches, workflows, video_info
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.project_batch_models import ProjectBatch
from dubforge.release_batch_models import now
from dubforge.publishing import service
from dubforge.publishing.models import PublishAccount
from dubforge.workflow_models import WorkflowRun
from dubforge.workflow_plan import digest


class ProjectBatchError(DubForgeError):
    code = 'PROJECT_BATCH_INVALID'
    suggestion = '请核对各项目保存的目标，重新检查后确认；已有产物和发布回执会保留。'


ACTIVE = {'running', 'pausing', 'stopping'}
GOAL_PATHS = {'run.mode', 'publish.project_target', 'publish.account_id', 'publish.season_id',
              'publish.section_id', 'publish.version_ids', 'publish.part_titles',
              'assembly.target_version_ids'}
_lock = threading.RLock()
_owner = threading.local()
_running = {}
_leases = {}
_children = {}
_closing = False
logger = logging.getLogger(__name__)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def stored_groups(raw):
    """损坏的旧展示数据不能拖垮所有项目卡片；实际启动仍用严格方案。"""
    try:
        groups = json.loads(raw)
        return [g for g in groups if isinstance(g,dict) and isinstance(g.get('project_uids'),list)
                and isinstance(g.get('items'),list)] if isinstance(groups,list) else []
    except (ValueError,TypeError):
        return []


def assert_version_goals(key, before, after):
    shape = lambda rows: [{name:row.get(name) for name in ('id','dubbing','burn_subtitles','include_comments',
                                                         'partial','update','timeline')} for row in rows]
    if not isinstance(before,list) or not isinstance(after,list) or any(not isinstance(row,dict) for row in [*before,*after]):
        return
    if shape(before) == shape(after):
        return
    from dubforge.runtime_edit import _projects
    with Session(get_engine()) as session:
        uids = set(_projects(session,key))
        for batch in session.exec(select(ProjectBatch).where(ProjectBatch.status.in_(ACTIVE))).all():
            if any(g.get('status') in ACTIVE and uids.intersection(g['project_uids']) for g in stored_groups(batch.groups)):
                raise ProjectBatchError('批量任务的成片目标已确认，请先暂停相应分组再增删版本或更换声音、字幕、评论及剪辑范围',
                                        code='PROJECT_BATCH_BUSY')


def _request(value):
    try:
        if not isinstance(value, str) or len(value) != 36:
            raise ValueError()
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ProjectBatchError('请求标识无效，请重新打开批量运行检查') from exc


def _uids(value):
    try:
        return release_batches._uids(value)
    except DubForgeError as exc:
        raise ProjectBatchError(exc.message) from exc


def _body(body):
    if not isinstance(body, dict) or any(key in body for key in (
            'scope', 'recipe', 'mode', 'version_ids', 'publication_ids', 'dubbing', 'overrides')):
        raise ProjectBatchError('批量运行只读取各项目保存的目标，请到步骤页设置后重新检查')
    return body


@contextmanager
def _owned(key):
    previous = getattr(_owner, 'key', None)
    _owner.key = key
    try:
        yield
    finally:
        _owner.key = previous


def owns(uid):
    return bool(_leases.get(uid) and _leases[uid] == getattr(_owner, 'key', None))


def child_owned(uid):
    child = _children.get(uid)
    if bool(child and child == getattr(workflows._owner, 'key', None)) or release_batches.owns(uid):
        return True
    parent_id = _leases.get(uid)
    nested_id = release_batches._leases.get(uid)
    if not parent_id or not nested_id or not release_batches.child_owned(uid):
        return False
    # 父批次 → 合集子批次 → 工作流分别在线程中运行；只认该分组实际登记的孙任务，
    # 不能把任意顺序批次的工作流当作本父批次的执行身份。
    from dubforge.release_batch_models import ReleaseBatch
    with Session(get_engine()) as session:
        parent = session.get(ProjectBatch, parent_id)
        nested = session.get(ReleaseBatch, nested_id)
        if not parent or parent.status not in ACTIVE or not nested or nested.status not in release_batches.ACTIVE:
            return False
        try:
            if json.loads(nested.plan).get('parent_batch_id') != parent_id:
                return False
        except (TypeError, ValueError, AttributeError):
            return False
        return any(group.get('kind') == 'collection' and group.get('child_id') == nested.id
                   and group.get('status') in ACTIVE and uid in group['project_uids']
                   for group in stored_groups(parent.groups))


def check_idle(uid):
    if _leases.get(uid) and not owns(uid) and not child_owned(uid):
        raise ProjectBatchError('项目属于正在执行的批量任务，请先暂停或停止相应分组', code='PROJECT_BATCH_BUSY')
    if _leases.get(uid) or child_owned(uid):
        return
    # 落盘状态先于内存租约，所有单项目启动入口也须覆盖这一小段窗口。
    with Session(get_engine()) as session:
        rows = session.exec(select(ProjectBatch).options(load_only(ProjectBatch.id,ProjectBatch.groups))
                            .where(ProjectBatch.status.in_(ACTIVE))).all()
        for row in rows:
            if getattr(_owner,'key',None) == row.id:
                continue
            if any(group.get('status') in ACTIVE and uid in group['project_uids'] for group in stored_groups(row.groups)):
                raise ProjectBatchError('项目已被批量任务预留，请先暂停或停止相应分组',code='PROJECT_BATCH_BUSY')


def bind_workflow(uid, key):
    if not owns(uid):
        raise ProjectBatchError('批量执行身份无效', code='PROJECT_BATCH_OWNER')
    _children[uid] = key


def assert_child_start(parent_id, child_id):
    batch = get(parent_id)
    group = next((g for g in batch['groups'] if g.get('child_id') == child_id), None)
    if group is None or group['status'] != 'running' or _closing:
        raise ProjectBatchError('批量分组已暂停或停止，不再启动子任务', code='PROJECT_BATCH_STOPPED')
    event = _running.get((parent_id,group['id']))
    if event is None or event.is_set() or not all(owns(uid) for uid in group['project_uids']):
        raise ProjectBatchError('批量分组执行身份无效', code='PROJECT_BATCH_OWNER')


@contextmanager
def launching(uid):
    with _lock:
        if not owns(uid):
            raise ProjectBatchError('批量执行身份无效', code='PROJECT_BATCH_OWNER')
        key = _leases[uid]
        group = next((g for g in get(key)['groups'] if uid in g['project_uids']), {})
        group_id = group.get('id', '')
        event = _running.get((key, group_id))
        if event is None or event.is_set() or _closing or group.get('status') != 'running':
            raise ProjectBatchError('分组已经停止，不再启动新项目', code='PROJECT_BATCH_STOPPED')
        yield


def _row(session, key):
    row = session.get(ProjectBatch, key)
    if row is None:
        raise ProjectBatchError('批量任务不存在', code='PROJECT_BATCH_NOT_FOUND')
    return row


def _view(row):
    data = row.model_dump(mode='json')
    for field in ('plan', 'groups'):
        data[field] = stored_groups(data[field]) if field == 'groups' else json.loads(data[field])
    for group in data['groups']:
        group['items'] = [release_batches._inactive_item_display(item, group.get('status')) for item in group['items']]
        for item in group['items']:
            if item.get('feedback') == 'preparing':
                # 历史诊断仍保留在落盘记录；本次准备期间不将旧失败当成当前故障。
                for name in ('failed_step', 'error_code', 'suggestion', 'failure'):
                    item.pop(name, None)
    data.pop('requests', None)
    data['fingerprint'] = data['plan'].get('fingerprint', '')
    if data['status'] == 'creation_failed':
        data['creation_error'] = data['plan'].get('creation_error') or dict(code='PROJECT_BATCH_CREATE_INTERRUPTED',message=data['error'])
    return data


def get(key):
    with Session(get_engine()) as session:
        return _view(_row(session, key))


def listing(request_id=None):
    with Session(get_engine()) as session:
        query = select(ProjectBatch).order_by(ProjectBatch.created_at.desc())
        if request_id:
            query = query.where(ProjectBatch.request_id == _request(request_id))
        else:
            query = query.where(ProjectBatch.status.not_in({'creating','creation_failed'})).options(load_only(
                ProjectBatch.id,ProjectBatch.request_id,ProjectBatch.status,ProjectBatch.phase,ProjectBatch.error,
                ProjectBatch.created_at,ProjectBatch.updated_at,ProjectBatch.groups))
        return {'batches': [(_view(row) if request_id else _summary(row))
                            for row in session.exec(query.limit(30)).all()]}


def _summary(row):
    result = {key:getattr(row,key) for key in ('id','request_id','status','phase','error','created_at','updated_at')}
    result['groups'] = []
    for group in stored_groups(row.groups):
        summary = {key:copy.deepcopy(group[key]) for key in ('id','kind','title','scope','scope_display',
            'status','phase','error','feedback','child_id','workflow_id','project_uids') if key in group}
        summary['items'] = [{key:item[key] for key in ('project_uid','title','source_date','status','phase','error','feedback') if key in item}
                            for item in (release_batches._inactive_item_display(value, group.get('status')) for value in group['items'])]
        result['groups'].append(summary)
    return result


def project_statuses(uids):
    """项目列表只查批次登记，不扫描产物或运行远程预检。"""
    selected, result = set(uids), {}
    if not selected:
        return result
    with Session(get_engine()) as session:
        rows = session.exec(select(ProjectBatch).options(load_only(ProjectBatch.id,ProjectBatch.status,ProjectBatch.groups))
                            .where(ProjectBatch.status.in_(ACTIVE | {'paused','attention'}))
                            .order_by(ProjectBatch.created_at.desc()).limit(100)).all()
        for row in rows:
            for group in stored_groups(row.groups):
                if group.get('status') == 'done':
                    continue
                for index, uid in enumerate(group['project_uids']):
                    if uid not in selected or uid in result:
                        continue
                    item = release_batches._inactive_item_display(
                        next((value for value in group['items'] if value.get('project_uid') == uid), {}), group.get('status'))
                    result[uid] = dict(batch_id=row.id, group_id=group['id'], status=row.status,
                        group_status=group['status'], title=group['title'], phase=group['phase'],
                        position=index + 1, total=len(group['project_uids']),
                        item_status=item.get('status', ''), item_phase=item.get('phase', ''),
                        feedback=item.get('feedback') or group.get('feedback', ''))
    return result


def _candidate(uid):
    project = crud.get_project(uid)
    if project is None:
        raise ProjectBatchError('所选项目不存在', code='PROJECT_NOT_FOUND')
    spec = configuration.run_spec(uid, target_driven=True)
    settings = configuration.resolve(uid)
    values = settings['values']
    blockers = [issue['message'] for issue in spec.get('project_goal_issues', [])]
    if project.archived_at is not None:
        blockers.append('项目已归档，请先恢复后运行')
    scope = {key: values['publish.' + key] for key in ('account_id', 'season_id', 'section_id')}
    with Session(get_engine()) as session:
        account = session.get(PublishAccount, scope['account_id']) if scope['account_id'] else None
        account_name = account.name if account else ''
    target = values.get('publish.project_target') or {}
    return dict(project_uid=uid, uid=uid, title=project.title, source_date=release_batches._source_date(uid),
        delivery_mode=spec.get('delivery_mode', spec['mode']), end=spec.get('delivery_mode', spec['mode']),
        recipe=spec, saved_scope=scope, target=target, account_name=account_name,
        target_selection=copy.deepcopy(settings.get('target_selection')),
        project_goal=dict(delivery_mode=spec.get('delivery_mode', spec['mode']), target_kind=target.get('kind', ''),
                          target_publication_id=target.get('publication_id', ''),
                          production_version_ids=spec.get('production_version_ids', []),
                          publish_version_ids=spec.get('version_ids', [])), blockers=blockers)


def candidates(uids):
    rows = [_candidate(uid) for uid in _uids(uids)]
    warnings = [release_batches.SOURCE_DATE_WARNING] if any(
        group['kind'] == 'collection' and len(group['items']) > 1
        and any(not item['source_date'] for item in group['items']) for group in _group_rows(rows)) else []
    return dict(items=rows, warnings=warnings)


def inspect_candidates(body):
    """补齐日期是用户发起的独立预检，不扩大创建、排序或恢复的副作用。"""
    body = _body(body)
    if set(body) - {'project_uids'}:
        raise ProjectBatchError('日期检查仅接受所选项目，请重新打开批量运行检查')
    uids = _uids(body.get('project_uids'))
    # 全部项目先通过只读检查，避免无效请求只更新前半批。
    candidates(uids)
    from dubforge import source_dates
    source_dates.ensure_many(uids)
    # 网络等待期间设置可能发生变化，仍按最新目标生成后续执行方案。
    return candidates(uids)


def _group_rows(rows):
    groups = {}
    for row in rows:
        scope = row['saved_scope']
        collection = row['delivery_mode'] == 'publish' and scope.get('season_id') is not None
        identity = dict(scope=scope) if collection else dict(project_uid=row['project_uid'])
        key = digest(identity)[:24]
        if key not in groups:
            groups[key] = dict(id=key, kind='collection' if collection else 'independent', scope=scope,
                account_name=row['account_name'], project_uids=[], items=[], title='合集顺序组' if collection else row['title'])
        groups[key]['project_uids'].append(row['project_uid'])
        groups[key]['items'].append(row)
    return list(groups.values())


def _independent_plan(row, *, online=True, saved=None):
    uid = row['project_uid']
    spec = copy.deepcopy(saved['recipe'] if saved else row['recipe'])
    run = workflows.get(saved['workflow_id']) if saved and saved.get('workflow_id') else None
    if run and run['status'] in {'completed', 'ready'}:
        return copy.deepcopy(saved['plan'])
    if run and run.get('publication_id'):
        pub = service.view(run['publication_id'])
        if pub['status'] == 'unknown':
            plan = copy.deepcopy(run['plan'])
            plan['blockers'] = ['投稿回执不明，请到发布页核对已有稿件；不会自动新建或重投']
            return plan
        if spec.get('mode') != 'update':
            if pub['receipt'].get('bvid') and pub['status'] not in {'rejected','collection_pending'}:
                plan = copy.deepcopy(run['plan'])
                plan['blockers'] = []
                plan['recovery'] = dict(kind='publication_monitor',publication_id=pub['id'],
                    revision=pub['revision'],status=pub['status'],receipt=digest(pub['receipt']))
                return plan
            plan = copy.deepcopy(run['plan'])
            plan['blockers'] = ['请先到发布页核对并继续现有稿件，不会新建重复稿件']
            return plan
    if run and spec.get('mode') == 'update' and workflows._update_task(run):
        task = workflows._update_task(run)
        plan = copy.deepcopy(run['plan'])
        plan['recipe'] = copy.deepcopy(run['recipe'])
        plan['recovery'],plan['recovery_fingerprint'] = workflows._update_recovery(run,task)
        plan['publication_update'],plan['blockers'] = task,[]
        if task['status'] == 'unknown':
            plan['_update_reconcile_once'] = True
        return plan
    plan = workflows.prepared_plan(uid, spec, online=online,
        dubbing_operation_id=run['id'] if run else None,
        previous_update=(saved or {}).get('plan', {}).get('update_plan'))
    if run:
        plan, _ = workflows._recovery_plan(run, plan)
    else:
        from dubforge.pipeline.recovery import describe
        plan['recovery'] = describe(workflows._plan_dependencies(crud.get_project(uid), plan), plan['steps'])
    issues = workflows._availability(uid, spec['mode'], run['id'] if run else None,
                                    confirm_new_publication=spec.get('confirm_new_publication', False),
                                    target_driven=spec.get('target_driven', False))
    if not run and spec['mode'] != 'produce':
        # 结果不明的历史稿件必须人工核对，不能借批量入口新建一份重投。
        from dubforge.publishing.models import Publication
        with Session(get_engine()) as session:
            if session.exec(select(Publication.id).where(Publication.project_uid == uid,
                                                        Publication.status == 'unknown')).first():
                issues.append('投稿回执不明，请先在发布页核对已有稿件；不会自动新建或重投')
    plan['blockers'] = list(dict.fromkeys([*plan.get('blockers', []), *issues, *row.get('blockers', [])]))
    return plan


def _fingerprint(groups):
    return digest([dict(id=g['id'], kind=g['kind'], project_uids=g['project_uids'],
                        fingerprint=g['plan']['fingerprint'], recovery=g['plan'].get('recovery'),
                        recovery_fingerprint=g['plan'].get('recovery_fingerprint'),
                        blockers=g['blockers']) for g in groups])


def _result(groups, *, selected=None):
    chosen = groups if selected is None else [g for g in groups if g['id'] in selected]
    return dict(groups=groups, group_ids=[g['id'] for g in chosen],
        blockers=[f'{g["title"]}：{reason}' for g in chosen for reason in g['blockers']],
        # 日期缺失由对应合集组就地提示，避免顶部重复且缺少分组上下文。
        warnings=list(dict.fromkeys(w for g in chosen for w in g.get('warnings', [])
                                    if w != release_batches.SOURCE_DATE_WARNING)),
        fingerprint=_fingerprint(chosen))


def preview(body, *, online=True):
    body = _body(body)
    groups = _group_rows(candidates(body.get('project_uids'))['items'])
    for group in groups:
        try:
            if group['kind'] == 'collection':
                plan = release_batches.preview(dict(project_uids=group['project_uids'], target_driven=True,
                                                    order_confirmed=body.get('order_confirmed')), online=online)
                group['scope_display'] = service.settings_display(group['scope']['account_id'], group['scope'])
                group['title'] = ' / '.join(filter(None, [group['scope_display'].get('account_name'),
                    group['scope_display'].get('season_title'), group['scope_display'].get('section_title')])) or '合集顺序组（名称待核对）'
            else:
                plan = _independent_plan(group['items'][0], online=online)
            group.update(plan=plan, blockers=plan.get('blockers', []), warnings=plan.get('warnings', []),
                         status='queued', phase='等待启动', error='', child_id='', workflow_id='')
            if group['kind'] == 'independent':
                group['items'][0].update(recipe=plan['recipe'], plan=plan, workflow_id='')
            else:
                group['items'] = [{**row, **item} for row, item in zip(group['items'], plan['items'])]
        except DubForgeError as exc:
            group.update(plan=dict(fingerprint=digest(dict(error=exc.code, message=exc.message))),
                blockers=[exc.message], warnings=[], status='queued', phase='预检未通过', error='', child_id='', workflow_id='')
    if body.get('order_confirmed') is not True:
        for group in groups:
            if group['kind'] == 'collection':
                group['blockers'] = list(dict.fromkeys([*group['blockers'], '请核对并确认合集组内顺序']))
    return _result(groups)


def create(body):
    body = _body(body)
    request = _request(body.get('request_id'))
    if body.get('confirmed') is not True:
        raise ProjectBatchError('请确认项目目标、合集分组和组内顺序')
    signature = digest(dict(project_uids=_uids(body.get('project_uids')), fingerprint=body.get('fingerprint'), confirmed=True))
    with _lock, Session(get_engine()) as session:
        old = session.exec(select(ProjectBatch).where(ProjectBatch.request_id == request)).first()
        if old:
            if json.loads(old.requests).get('_creation') != signature:
                raise ProjectBatchError('同一创建请求不能提交不同项目或方案', code='PROJECT_BATCH_CONFLICT')
            result = _view(old)
            return _ensure_children(result) if old.status not in {'creating','creation_failed'} else result
        # 在慢预检前持久占有UUID；超时重试能区分仍在创建和已终结失败。
        row = ProjectBatch(request_id=request,status='creating',phase='正在核对已确认方案',
            plan=_json(dict(fingerprint=body.get('fingerprint'))),requests=_json({'_creation':signature}))
        session.add(row)
        session.commit()
        session.refresh(row)
        key = row.id
    try:
        candidate = preview(body)
        if candidate['blockers'] or body.get('fingerprint') != candidate['fingerprint']:
            raise ProjectBatchError('检查未通过或方案已变化，请重新检查', code='PROJECT_BATCH_PLAN_CHANGED',detail=candidate['blockers'])
        with _lock,Session(get_engine()) as session:
            row = _row(session,key)
            if row.status != 'creating':
                # 重启/终结回执已经落盘，旧线程晚到也不能把UUID复活成可执行任务。
                return _view(row)
            row.plan,row.groups,row.status,row.phase = _json(candidate),_json(candidate['groups']),'ready','已创建，等待确认开始'
            row.updated_at = now()
            session.add(row)
            session.commit()
            session.refresh(row)
            result = _view(row)
    except Exception as exc:
        error = exc if isinstance(exc,DubForgeError) else ProjectBatchError('创建检查未完成，请重新检查',code='PROJECT_BATCH_CREATE_FAILED')
        with _lock,Session(get_engine()) as session:
            row = _row(session,key)
            if row.status == 'creating':
                row.status,row.phase,row.error = 'creation_failed','创建已终结，未启动任何任务',error.message
                row.plan = _json(dict(fingerprint=body.get('fingerprint'),creation_error=dict(code=error.code,message=error.message)))
                row.updated_at = now()
                session.add(row)
                session.commit()
        raise error
    # 创建只登记本地子批次，冻结配方；未开始前不制作、不上传、不投稿。
    return _ensure_children(result)


def _ensure_children(result):
    for group in result['groups']:
        if group['kind'] == 'collection':
            child = release_batches.create_prepared(group['plan'], _child_request(result['id'],group['id']), result['id'])
            _save_group(result['id'],group['id'],child_id=child['id'])
    return get(result['id'])


def _child_request(key, group_id):
    return str(uuid.uuid5(uuid.UUID(hex=key), group_id))


def _discover_child(key, group):
    request = _child_request(key, group['id'])
    if group['kind'] == 'collection':
        rows = release_batches.listing(request)['batches']
        if rows:
            return dict(child_id=rows[0]['id'])
    else:
        with Session(get_engine()) as session:
            row = session.exec(select(WorkflowRun).where(WorkflowRun.request_id == request)).first()
            if row:
                return dict(workflow_id=row.id)
    return {}


def _selection(groups, ids):
    valid = {g['id'] for g in groups}
    if ids is None:
        return [g['id'] for g in groups if g['status'] not in ACTIVE | {'done'}]
    if not isinstance(ids, list) or not ids or any(not isinstance(v, str) or v not in valid for v in ids) or len(set(ids)) != len(ids):
        raise ProjectBatchError('请选择当前批次中的有效分组')
    return ids


def recovery_plan(key, body=None, *, online=True):
    body = _body(body or {})
    batch = get(key)
    if batch['status'] in {'creating','creation_failed'}:
        raise ProjectBatchError('批次尚未成功创建，请等待创建结果或重新检查',code='PROJECT_BATCH_CREATE_INCOMPLETE')
    selected = _selection(batch['groups'], body.get('group_ids'))
    groups = copy.deepcopy(batch['groups'])
    for group in groups:
        if group['id'] not in selected:
            continue
        if group['status'] in ACTIVE | {'done'}:
            raise ProjectBatchError('所选分组正在运行或已经完成，请刷新状态')
        group.update(_discover_child(key, group))
        if group['kind'] == 'collection':
            if group.get('child_id'):
                child = release_batches.get(group['child_id'])
                if child['status'] == 'done':
                    group.update(status='done', phase=child['phase'], error='', blockers=[])
                    continue
                plan = release_batches.recovery_plan(child['id'], online=online)
            else:
                # 尚未创建子批次时必须核对冻结的分组；不得把项目移动到另一个合集。
                # 创建回执晚到时仍用原冻结方案登记同一子任务，不能读新目标重建。
                child = release_batches.create_prepared(group['plan'],_child_request(key,group['id']),key)
                group['child_id'] = child['id']
                plan = release_batches.recovery_plan(child['id'],online=online)
            group.update(plan=plan, blockers=plan['blockers'], warnings=plan.get('warnings', []))
        else:
            item = group['items'][0]
            item['workflow_id'] = group.get('workflow_id', '')
            plan = _independent_plan(item, online=online, saved=item)
            group.update(plan=plan, blockers=plan['blockers'], warnings=plan.get('warnings', []))
            item['plan'] = plan
    return _result(groups, selected=selected)


def _save_group(key, group_id, **fields):
    with _lock, Session(get_engine()) as session:
        row = _row(session, key)
        groups = json.loads(row.groups)
        selected = next(group for group in groups if group['id'] == group_id)
        if fields.get('status') and fields['status'] != 'running':
            fields.setdefault('feedback', '')
            # 准备被取消或核验失败后，展示状态也必须收尾，不能一直显示正在继续。
            items = copy.deepcopy(fields.get('items', selected['items']))
            for item in items:
                if item.get('feedback') == 'preparing':
                    item['feedback'] = ''
                    item['phase'] = '等待重新检查后继续'
            fields['items'] = items
        if all(selected.get(name) == value for name,value in fields.items()):
            return
        selected.update(fields)
        statuses = {group['status'] for group in groups}
        row.status = ('running' if statuses & {'running'} else 'stopping' if statuses & {'stopping'}
            else 'pausing' if statuses & {'pausing'} else 'done' if statuses == {'done'}
            else 'attention' if statuses & {'attention'} else 'paused' if statuses & {'paused'}
            else 'stopped' if statuses & {'stopped'} else 'ready')
        row.groups, row.updated_at = _json(groups), now()
        row.phase = ('全部项目已完成' if row.status == 'done' else
            f'已完成 {sum(g["status"] == "done" for g in groups)}/{len(groups)} 组；各组独立推进')
        row.error = '；'.join(g['error'] for g in groups if g.get('error'))
        session.add(row)
        session.commit()


def _conflict(key, groups):
    wanted_uids = {uid for g in groups for uid in g['project_uids']}
    scopes = {digest(g['scope']) for g in groups if g['kind'] == 'collection'}
    with Session(get_engine()) as session:
        for row in session.exec(select(ProjectBatch).where(ProjectBatch.status.in_(ACTIVE))).all():
            if row.id == key:
                continue
            for group in json.loads(row.groups):
                if group['status'] not in ACTIVE:
                    continue
                if (wanted_uids.intersection(group['project_uids']) or
                    group['kind'] == 'collection' and digest(group['scope']) in scopes):
                    raise ProjectBatchError('所选项目或相同合集小节已被另一批次占用', code='PROJECT_BATCH_BUSY')
    for group in groups:
        if group['kind'] == 'collection':
            release_batches._conflict(group['scope'], group['project_uids'], group.get('child_id', ''))


def start(key, body):
    body = _body(body)
    request = _request(body.get('request_id'))
    if body.get('confirmed') is not True:
        raise ProjectBatchError('请确认各项目交付目标、合集顺序和相应发布权利')
    signature = digest(dict(fingerprint=body.get('fingerprint'), group_ids=body.get('group_ids'),
                            confirmed=True, rights_confirmed=body.get('rights_confirmed') is True))
    with _lock, Session(get_engine()) as session:
        row = _row(session, key)
        receipts = json.loads(row.requests)
        if request in receipts:
            if receipts[request] != signature:
                raise ProjectBatchError('同一启动请求不能确认不同方案', code='PROJECT_BATCH_CONFLICT')
            return _view(row)
        if _closing:
            raise ProjectBatchError('服务正在关闭，请重启后重新确认')
    candidate = recovery_plan(key, body)
    submitting = any(item.get('delivery_mode', item.get('recipe',{}).get('delivery_mode')) == 'publish'
        for group in candidate['groups'] if group['id'] in candidate['group_ids'] for item in group['items'])
    if submitting and body.get('rights_confirmed') is not True:
        raise ProjectBatchError('本次包含投稿项目，请明确确认相应发布权利')
    if not candidate['group_ids'] or candidate['blockers'] or candidate['fingerprint'] != body.get('fingerprint'):
        raise ProjectBatchError('恢复检查已变化或未通过，请重新检查', code='PROJECT_BATCH_PLAN_CHANGED', detail=candidate['blockers'])
    from dubforge import runtime_edit
    from dubforge.runner import runner
    from dubforge.pipeline import dubbing_session
    with service._lock, runner._start_lock, dubbing_session.gate, video_info.gate, _lock:
        batch = get(key)
        with Session(get_engine()) as session:
            row = _row(session, key)
            receipts = json.loads(row.requests)
            if request in receipts:
                if receipts[request] != signature:
                    raise ProjectBatchError('同一启动请求不能确认不同方案', code='PROJECT_BATCH_CONFLICT')
                return _view(row)
        chosen = [g for g in candidate['groups'] if g['id'] in candidate['group_ids']]
        if any((key,g['id']) in _running or next(b for b in batch['groups'] if b['id'] == g['id'])['status'] in ACTIVE for g in chosen):
            raise ProjectBatchError('分组状态已变化，请刷新', code='PROJECT_BATCH_BUSY')
        _conflict(key, chosen)
        for group in chosen:
            for uid in group['project_uids']:
                runtime_edit.check_idle(uid)
        # 本地复核不用重新发起远端读取，保持启动锁短小；子执行器仍有自己的最终复核。
        for group in chosen:
            if group['kind'] == 'independent':
                item = group['items'][0]
                current = _independent_plan(item, online=False, saved=item)
                if current['blockers'] or current['fingerprint'] != group['plan']['fingerprint']:
                    raise ProjectBatchError('检查期间设置或文件发生变化，请重新确认', code='PROJECT_BATCH_PLAN_CHANGED')
        for group in chosen:
            group.update(status='running', phase='已受理，正在准备继续任务', error='', feedback='preparing')
            for item in group['items']:
                if item.get('status') not in {'done', 'completed'}:
                    item.update(status='queued', phase='正在核对已有结果与执行条件', error='', feedback='preparing')
        receipts[request] = signature
        with Session(get_engine()) as session:
            row = _row(session, key)
            row.requests, row.plan = _json(receipts), _json(candidate)
            row.groups, row.status, row.updated_at = _json(candidate['groups']), 'running', now()
            # 分组准备状态与父摘要须同一事务更新，避免旧聚合错误被当成父级新故障。
            row.error = '；'.join(group['error'] for group in candidate['groups'] if group.get('error'))
            row.phase = (f'已完成 {sum(group["status"] == "done" for group in candidate["groups"])}/'
                         f'{len(candidate["groups"])} 组；正在准备继续 {len(chosen)} 组')
            session.add(row)
            session.commit()
        for group in chosen:
            event = threading.Event()
            _running[(key,group['id'])] = event
            for uid in group['project_uids']:
                _leases[uid] = key
                if group.get('workflow_id'):
                    _children[uid] = group['workflow_id']
            # 等待审核不占用工作流程/发布子执行器的线程池槽位。
            thread = threading.Thread(target=_execute, args=(key, group['id'], request, event), daemon=True,
                                      name='project-batch-' + group['id'][:8])
            try:
                thread.start()
            except Exception as exc:
                _release(key, group['id'])
                _save_group(key, group['id'], status='attention', phase='未能启动后台调度', error='请重新检查后继续本组')
    return get(key)


def _release(key, group_id):
    with _lock:
        _running.pop((key,group_id), None)
        group = next(g for g in get(key)['groups'] if g['id'] == group_id)
        for uid in group['project_uids']:
            if _leases.get(uid) == key:
                _leases.pop(uid, None)
                _children.pop(uid, None)


def _execute(key, group_id, start_request, event):
    try:
        with _owned(key):
            group = next(g for g in get(key)['groups'] if g['id'] == group_id)
            if event.is_set() or _closing:
                raise ProjectBatchError('分组已停止', code='PROJECT_BATCH_STOPPED')
            if group['kind'] == 'collection':
                child_id = group.get('child_id')
                if not child_id:
                    child = release_batches.create_prepared(group['plan'],_child_request(key,group_id),key)
                    child_id = child['id']
                    _save_group(key,group_id,child_id=child_id)
                child = release_batches.get(child_id)
                if child['status'] not in release_batches.ACTIVE | {'done'}:
                    with _lock:
                        current_group = next(g for g in get(key)['groups'] if g['id'] == group_id)
                        if event.is_set() or current_group['status'] != 'running' or _closing:
                            raise ProjectBatchError('分组已经暂停或停止，不再启动子任务',code='PROJECT_BATCH_STOPPED')
                    # 子 start 自己会在线核对这个已确认指纹，并在锁内做最终本地校验；
                    # 父执行器不再紧接着重复读取一遍远端合集与所有项目的恢复方案。
                    release_batches.start(child_id, dict(confirmed=True, rights_confirmed=True, fingerprint=group['plan']['fingerprint'],
                        request_id=str(uuid.uuid5(uuid.UUID(start_request),group_id))))
                while True:
                    child = release_batches.get(child_id)
                    if child['status'] not in release_batches.ACTIVE:
                        status = 'done' if child['status'] == 'done' else 'paused' if child['status'] == 'paused' else 'stopped' if child['status'] == 'stopped' else 'attention'
                        _save_group(key,group_id,status=status,phase=child['phase'],error=child['error'],items=child['items'])
                        break
                    _save_group(key,group_id,phase=child['phase'],items=child['items'],
                                feedback='preparing' if any(i.get('feedback') == 'preparing' for i in child['items']) else '')
                    if event.wait(.5):
                        release_batches.stop(child_id)
                        time.sleep(.05)
            else:
                item = group['items'][0]
                uid, workflow_id = item['project_uid'], group.get('workflow_id', '')
                if workflow_id:
                    run = workflows.get(workflow_id)
                    if run['status'] in {'completed','ready'}:
                        item.update(status='done',phase=run['phase'])
                        _save_group(key,group_id,status='done',phase=run['phase'],error='',workflow=run,items=[item])
                        return
                else:
                    # 保留确认的目标配方；普通参数由步骤在真正消费时读取。
                    current = workflows.prepared_plan(uid, item['recipe'], online=False,
                        previous_update=item['plan'].get('update_plan'))
                    if current['blockers']:
                        raise ProjectBatchError('；'.join(current['blockers']))
                    publication_id = item['recipe'].get('update', {}).get('publication_id', '')
                    run = workflows.start_prepared(uid,current,_child_request(key,group_id),
                        publication_id=publication_id,frozen_target=True)
                    workflow_id = run['id']
                    _save_group(key,group_id,workflow_id=workflow_id)
                if run['status'] in {'failed','paused','attention'}:
                    plan = group['plan']
                    workflows.resume(workflow_id,dict(confirmed=True,recipe=item['recipe'],fingerprint=plan['fingerprint'],
                        recovery_fingerprint=plan.get('recovery_fingerprint'),
                        request_id=str(uuid.uuid5(uuid.UUID(start_request),group_id))))
                while True:
                    run = workflows.get(workflow_id)
                    item.update(status=run['status'], phase=run['phase'], error='', feedback='')
                    _save_group(key,group_id,phase=run['phase'],workflow=run,items=[item],feedback='')
                    if run['status'] in {'completed','ready','failed','paused','attention'}:
                        current_group = next(g for g in get(key)['groups'] if g['id'] == group_id)
                        terminal = ('done' if run['status'] in {'completed','ready'} else
                            'paused' if current_group['status'] == 'pausing' else
                            'stopped' if current_group['status'] == 'stopping' or event.is_set() else 'attention')
                        item.update(status='done' if run['status'] in {'completed','ready'} else run['status'],phase=run['phase'])
                        _save_group(key,group_id,status=terminal,
                            error=run['error'],phase=run['phase'],items=[item])
                        break
                    if event.wait(.5):
                        workflows.stop(workflow_id)
                        time.sleep(.05)
    except Exception as exc:
        if not isinstance(exc,DubForgeError):
            logger.exception('混合批次 %s 分组 %s 执行失败',key,group_id)
        message = exc.message if isinstance(exc,DubForgeError) else '本组未完成，请查看项目任务和发布记录后继续'
        current_group = next(g for g in get(key)['groups'] if g['id'] == group_id)
        status = 'paused' if current_group['status'] == 'pausing' else 'stopped' if event.is_set() or _closing else 'attention'
        _save_group(key,group_id,status=status,error=message,phase='本组已暂停，其他组继续')
    finally:
        _release(key,group_id)


def _control(key, body, *, stop):
    from dubforge.runner import runner
    # 和子执行器最终入队使用相同门禁，暂停确认不能落在检查与入队之间。
    with service._lock, runner._start_lock:
        return _control_locked(key,body,stop=stop)


def _control_locked(key, body, *, stop):
    body = body or {}
    group_id = body.get('group_id')
    batch = get(key)
    groups = [g for g in batch['groups'] if not group_id or g['id'] == group_id]
    if not groups:
        raise ProjectBatchError('所选分组不存在')
    for group in groups:
        if group['status'] == 'done':
            continue
        event = _running.get((key,group['id']))
        child_pending = group.get('child_id') and release_batches.get(group['child_id'])['status'] not in release_batches.ACTIVE
        if event and (stop or child_pending or not group.get('child_id') and not group.get('workflow_id')):
            event.set()
        if group.get('child_id'):
            (release_batches.stop if stop else release_batches.pause)(group['child_id'])
        elif group.get('workflow_id'):
            workflows.stop(group['workflow_id'])
        status = ('stopping' if stop else 'pausing') if event else ('stopped' if stop else 'paused')
        _save_group(key,group['id'],status=status,phase='正在等待本组收尾' if event else '分组已停止' if stop else '分组已暂停')
    return get(key)


def pause(key, body=None):
    return _control(key,body,stop=False)


def stop(key, body=None):
    return _control(key,body,stop=True)


def recover():
    global _closing
    with _lock, Session(get_engine()) as session:
        _closing = False
        for row in session.exec(select(ProjectBatch).where(ProjectBatch.status.in_(ACTIVE | {'creating'}))).all():
            if row.status == 'creating':
                row.status,row.phase,row.error = 'creation_failed','创建检查因重启而终结','请重新检查并创建；旧请求不会自动继续'
                row.plan = _json(dict(creation_error=dict(code='PROJECT_BATCH_CREATE_INTERRUPTED',message=row.error)))
                row.updated_at = now()
                session.add(row)
                continue
            groups = json.loads(row.groups)
            for group in groups:
                if group['status'] in ACTIVE:
                    group.update(status='paused',phase='程序重启，等待重新检查确认',error='不会自动上传、投稿或排序',feedback='')
                    for item in group['items']:
                        if item.get('feedback') == 'preparing':
                            item.update(feedback='', phase='等待重新检查后继续')
                        if item.get('status') in {'running', 'monitoring'}:
                            item.update(status='paused', phase='程序重启，等待重新检查确认')
                    # 全局流程监控不能绕过父批次的重启暂停继续章节等后处理。
                    # 子登记回执丢失时也按确定UUID找回同一任务，不创建新任务。
                    discovered = _discover_child(row.id,group)
                    group.update(discovered)
                    workflow_ids = [group['workflow_id']] if group.get('workflow_id') else []
                    if group.get('child_id'):
                        from dubforge.release_batch_models import ReleaseBatch
                        child = session.get(ReleaseBatch,group['child_id'])
                        if child:
                            workflow_ids.extend(item.get('workflow_id') for item in json.loads(child.items) if item.get('workflow_id'))
                    for workflow_id in workflow_ids:
                        workflow = session.get(WorkflowRun,workflow_id)
                        if workflow is None or workflow.status in {'completed','ready'}:
                            continue
                        workflow.post_processing_paused = True
                        if workflow.status == 'monitoring':
                            workflow.status,workflow.phase = 'attention','批量任务因重启暂停，请重新检查本组后继续'
                            workflow.next_check = 0
                        session.add(workflow)
            row.groups,row.status,row.phase,row.updated_at = _json(groups),'paused','程序重启后已暂停',now()
            session.add(row)
        session.commit()


def shutdown():
    global _closing
    with _lock:
        _closing = True
        for event in _running.values():
            event.set()
