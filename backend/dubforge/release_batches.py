"""持久化顺序发布：一个批次串行推进，网页关闭不影响，重启须显式确认。"""
import copy
import datetime
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from sqlmodel import Session, select

from dubforge import configuration, video_info, workflows
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.domain.models import Project
from dubforge.errors import DubForgeError
from dubforge.publishing import service, studio
from dubforge.publishing.models import Publication
from dubforge.release_batch_models import ReleaseBatch, now
from dubforge.workflow_models import WorkflowRun
from dubforge.workflow_plan import digest


class ReleaseBatchError(DubForgeError):
    code = 'RELEASE_BATCH_INVALID'
    suggestion = '请检查当前项目、稿件与合集状态，重新预检并确认；不会自动跳过或重复投稿。'


ACTIVE = {'running', 'pausing', 'stopping'}
_lock = threading.RLock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='release-batch')
_running = {}
_leases = {}
_children = {}
_owner = threading.local()
_closing = False
logger = logging.getLogger(__name__)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _uids(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= 50
            or any(not isinstance(v, str) or not v or len(v) > 100 for v in value)
            or len(set(value)) != len(value)):
        raise ReleaseBatchError('请选择1–50个不重复的项目')
    return value


def _scope(value):
    allowed = {'account_id', 'season_id', 'section_id', 'season_title', 'section_title'}
    if (not isinstance(value, dict) or set(value) - allowed
            or not {'account_id', 'season_id', 'section_id'} <= set(value)
            or not isinstance(value['account_id'], str) or not value['account_id'] or len(value['account_id']) > 100
            or any(type(value[k]) is not int or value[k] <= 0 for k in ('season_id', 'section_id'))):
        raise ReleaseBatchError('请选择同一个发布账号、合集和小节')
    result = {key: value[key] for key in ('account_id', 'season_id', 'section_id')}
    # 名称只是当时读取到的显示快照，不参与远端调用或业务身份判断；
    # 允许批次历史在B站改名后仍显示当时可读的名称，同时保留数字 ID 供核对。
    for key in ('season_title', 'section_title'):
        label = value.get(key)
        if label is not None:
            if not isinstance(label, str) or not label.strip() or len(label) > 200:
                raise ReleaseBatchError('合集或小节名称无效，请刷新后重新选择')
            result[key] = label.strip()
    return result


def _scope_identity(scope):
    """只取平台对象身份；显示名称不能进入远端接口参数或确认指纹。"""
    return {key: scope[key] for key in ('account_id', 'season_id', 'section_id')}


def _remote_scope(scope):
    """去掉仅供界面显示的名称后调用发布服务。"""
    return _scope_identity(scope)


def _scope_display(value):
    """校验合集展示名称；它只用于界面，不参与发布目标或方案指纹。"""
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {'season_title', 'section_title'}:
        raise ReleaseBatchError('合集展示信息无效，请刷新合集列表后重试')
    result = {}
    for key in ('season_title', 'section_title'):
        title = value.get(key, '')
        if title is None:
            title = ''
        if not isinstance(title, str) or len(title.strip()) > 200:
            raise ReleaseBatchError('合集名称过长或格式无效，请刷新合集列表后重试')
        if title.strip():
            result[key] = title.strip()
    return result


def _request(value):
    try:
        if not isinstance(value, str) or len(value) != 36:
            raise ValueError()
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ReleaseBatchError('请求标识无效，请重新打开预检') from exc


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
    """工作流线程只继承本批次为该项目登记的精确子任务身份。"""
    child = _children.get(uid)
    return bool(_leases.get(uid) and child and child == getattr(workflows._owner, 'key', None))


def owns_publication(publication_id):
    """返回活动批次是否正在独占核对此稿件。

    批次在审核等待阶段会直接调用 ``refresh_status``。全局 workflow
    monitor 不能同时 enqueue ``check``，否则两个读写路径可能交错覆盖
    回执或重复请求平台；批次结束后再交还给全局 monitor。
    """
    if not publication_id:
        return False
    with _lock:
        for key in _running:
            batch = get(key)
            if any(item.get('publication_id') == publication_id
                   and item.get('status') in {'running', 'monitoring'}
                   for item in batch.get('items', [])):
                return True
    return False


def _lease_context(uid, key):
    """读取批次租约的人类可读上下文；失败时保持锁定语义。"""
    try:
        with Session(get_engine()) as session:
            row = session.get(ReleaseBatch, key)
            if row is None:
                return {}
            items = json.loads(row.items)
            index = next((index for index, item in enumerate(items)
                          if item.get('project_uid') == uid), None)
            if index is None:
                return {}
            item = items[index]
            return dict(batch_id=row.id, status=row.status, position=index + 1,
                        total=len(items), title=item.get('title') or uid,
                        item_status=item.get('status') or 'queued',
                        phase=item.get('phase') or '')
    except Exception:
        # 租约本身来自内存，查询失败不能把占用错误地当成可编辑。
        return {}


def _lease_error(uid, key):
    context = _lease_context(uid, key)
    if not context:
        return '项目属于正在执行的顺序发布批次，请先暂停或停止批次'
    status = context['status']
    stage = {'running': '正在处理当前项目', 'pausing': '当前项目完成后暂停',
             'stopping': '正在停止当前项目'}.get(status, '正在执行')
    if context['item_status'] == 'queued':
        stage = '等待前项完成'
    progress = f"第 {context['position']}/{context['total']} 项"
    phase = context['phase']
    detail = f"，{phase}" if phase and phase != stage else ''
    return (f"项目已锁定在顺序发布批次中（{progress}，{stage}{detail}），"
            '暂不能单独运行步骤、自动流程或修改制作资料；可查看批次进度，批次暂停或停止后再操作')


def check_idle(uid):
    lease = _leases.get(uid)
    if lease and not owns(uid) and not child_owned(uid):
        raise ReleaseBatchError(_lease_error(uid, lease), code='RELEASE_BATCH_BUSY')


def bind_workflow(uid, key):
    if not owns(uid):
        raise ReleaseBatchError('批次执行身份无效', code='RELEASE_BATCH_OWNER')
    _children[uid] = key


@contextmanager
def launching(uid):
    """与停止共用最终入队短锁；只能由真实批次线程进入。"""
    with _lock:
        if not owns(uid):
            raise ReleaseBatchError('批次执行身份无效', code='RELEASE_BATCH_OWNER')
        key = _leases[uid]
        event = _running.get(key)
        if event is None or event.is_set() or _closing or get(key)['status'] != 'running':
            raise ReleaseBatchError('批次已暂停或停止，不再启动新项目', code='RELEASE_BATCH_STOPPED')
        yield


def _row(session, key):
    row = session.get(ReleaseBatch, key)
    if row is None:
        raise ReleaseBatchError('发布批次不存在', code='RELEASE_BATCH_NOT_FOUND')
    return row


def _inactive_item_display(item, batch_status):
    """暂停调度器的历史快照不再代表当前项目；单项目可能已经另外恢复。"""
    value = dict(item)
    if batch_status in {'paused', 'stopped', 'attention'} and value.get('status') in {'running', 'monitoring'}:
        value.update(status=batch_status, feedback='', phase={
            'paused': '批次已暂停，等待重新检查', 'stopped': '批次已停止，已有结果保留',
            'attention': '批次等待处理，请核对项目当前状态'}[batch_status])
    return value


def _item_display(item, batch_status=None):
    """排队项尚未开始本次执行，不能展示被复用流程的历史错误和阶段。"""
    value = dict(item)
    if value.get('feedback') == 'preparing':
        value['error'] = ''
        for name in ('failed_step', 'error_code', 'suggestion', 'failure'):
            value.pop(name, None)
    if value.get('status') == 'queued' and value.get('feedback') != 'preparing':
        value.update(error='', phase='等待前项完成')
    return _inactive_item_display(value, batch_status)


def _view(row, progress=None):
    from dubforge import release_batch_progress
    if progress is None:
        with Session(get_engine()) as session:
            progress = release_batch_progress.load(session, [row])
    data = row.model_dump(mode='json')
    for name in ('scope', 'plan', 'items', 'requests'):
        data[name] = json.loads(data[name])
    data.pop('requests')
    data['fingerprint'] = data['plan'].get('fingerprint', '')
    data['scope_display'] = data['plan'].get('scope_display') or {}
    data['total'] = len(data['items'])
    data['items'] = [_item_display(item, row.status) for item in data['items']]
    for item in data['items']:
        if item.get('feedback') != 'preparing' and item.get('status') in {'running', 'monitoring'} and not (item.get('publication') or {}).get('reuse_existing'):
            run = progress['briefs'].get(item.get('workflow_id'))
            if run and run.project_uid == item.get('project_uid'):
                item['phase'] = run.phase if run.status != 'queued' else '等待流程开始'
                item['publication_id'] = run.publication_id or item.get('publication_id', '')
    data['current_item'] = progress['current'].get(row.id)
    return data


def get(key):
    from dubforge import release_batch_progress
    with Session(get_engine()) as session:
        row = _row(session, key)
        return _view(row, release_batch_progress.load(session, [row]))


def listing(request_id=None):
    from dubforge import release_batch_progress
    with Session(get_engine()) as session:
        query = select(ReleaseBatch).order_by(ReleaseBatch.created_at.desc()).limit(100)
        if request_id:
            query = query.where(ReleaseBatch.request_id == _request(request_id))
        rows = session.exec(query).all()
        progress = release_batch_progress.load(session, rows)
        return {'batches': [_summary(row, progress['briefs'], progress['current']) for row in rows]}


def _summary(row, briefs, current=None):
    fields = ('id', 'status', 'cursor', 'phase', 'error', 'created_at', 'updated_at')
    result = {key: value for key, value in row.model_dump(mode='json').items() if key in fields}
    result['scope'] = json.loads(row.scope)
    plan = json.loads(row.plan)
    result['scope_display'] = plan.get('scope_display') or {}
    result['fingerprint'] = plan.get('fingerprint', '')
    result['parent_batch_id'] = plan.get('parent_batch_id', '')
    item_fields = {
        'project_uid', 'title', 'source_date', 'status', 'phase', 'error',
        'workflow_id', 'publication_id', 'failed_step', 'error_code',
        'suggestion', 'failure', 'feedback',
    }
    result['items'] = []
    for item in json.loads(row.items):
        item = _item_display(item, row.status)
        brief = {key: value for key, value in item.items() if key in item_fields}
        run = briefs.get(item.get('workflow_id'))
        if (item.get('feedback') != 'preparing' and item.get('status') in {'running', 'monitoring'} and run
                and run.project_uid == item.get('project_uid')
                and not (item.get('publication') or {}).get('reuse_existing')):
            brief.update(phase=run.phase if run.status != 'queued' else '等待流程开始',
                         publication_id=run.publication_id or brief.get('publication_id', ''))
        result['items'].append(brief)
    result['total'] = len(result['items'])
    result['current_item'] = (current or {}).get(row.id)
    return result


def project_statuses(uids):
    """项目列表批量读取批次与当前流程简要，不扫描文件，也不读取远端平台。"""
    result = {}
    phase_sources = {}
    members = set(uids)
    if not members:
        return result
    with Session(get_engine()) as session:
        # 项目列表只需要这四列；批次 plan/items 可能包含完整冻结配方和
        # 恢复说明，不能为每个项目页把整行大 JSON 全量 ORM 实例化。
        # 活动批次按创建时间只保留最近一小段，异常遗留的活动行由恢复
        # 流程另行处理，不应拖垮摘要接口。
        rows = session.exec(select(ReleaseBatch.id, ReleaseBatch.status,
                                   ReleaseBatch.phase, ReleaseBatch.items)
                            .where(ReleaseBatch.status.in_(ACTIVE | {'ready', 'paused', 'attention', 'stopped', 'failed'}))
                            .order_by(ReleaseBatch.created_at.desc()).limit(100)).all()
        for row in rows:
            raw_items = row._mapping.get('items') if hasattr(row, '_mapping') else None
            try:
                items = json.loads(raw_items)
            except (TypeError, ValueError):
                # 坏批次记录不能让整个项目列表 500；详情接口仍可单独报告
                # 数据损坏，后台恢复不会凭坏 JSON 猜测项目占用。
                continue
            if not isinstance(items, list):
                continue
            for index, item in enumerate(items):
                if not isinstance(item, dict) or not isinstance(item.get('project_uid'), str):
                    continue
                uid = item['project_uid']
                if uid in members and item.get('status') != 'done' and uid not in result:
                    # 项目页需要知道自己是在等待前项，还是正在处理当前项；
                    # 批次总阶段不能代替条目阶段（尤其第 2 项排队时）。
                    item = _item_display(item, row.status)
                    mapping = row._mapping
                    result[uid] = dict(id=mapping['id'], status=mapping['status'], position=index + 1,
                                       total=len(items), phase=item.get('phase') or mapping['phase'],
                                       item_status=item.get('status', 'queued'),
                                       feedback=item.get('feedback', ''),
                                       batch_phase=mapping['phase'],
                                       lock_scope='queued' if item.get('status') == 'queued' else 'current')
                    publication = item.get('publication')
                    if publication is None:
                        publication = {}
                    workflow_id = item.get('workflow_id')
                    if (mapping['status'] in ACTIVE and item.get('status') in {'running', 'monitoring'}
                            and item.get('feedback') != 'preparing' and isinstance(publication, dict)
                            and not publication.get('reuse_existing') and isinstance(workflow_id, str) and workflow_id):
                        phase_sources[uid] = workflow_id
        # 条目落盘阶段只在调度边界更新；批量读取四个轻量字段，跟随明确归属的当前流程。
        # 不装载完整流程plan/recipe，不为每个项目单独查询，也不改变历史批次记录。
        if phase_sources:
            briefs = {run.id: run for run in session.exec(select(WorkflowRun.id, WorkflowRun.project_uid,
                WorkflowRun.status, WorkflowRun.phase).where(WorkflowRun.id.in_(set(phase_sources.values())))).all()}
            for uid, workflow_id in phase_sources.items():
                run = briefs.get(workflow_id)
                if (run and run.project_uid == uid and run.status in {'queued', 'running', 'stopping', 'monitoring'}):
                    result[uid]['phase'] = ('等待流程开始' if run.status == 'queued'
                                            else run.phase or result[uid]['phase'])
    return result


def project_status(uid):
    return project_statuses([uid]).get(uid)


def _end_preparation(items):
    """准备阶段的暂态反馈随暂停、失败或重启结束，历史步骤状态保持不变。"""
    values = copy.deepcopy(items)
    for item in values:
        if item.get('feedback') == 'preparing':
            item.update(feedback='', phase='等待重新检查后继续')
    return values


def _update(key, **fields):
    with _lock, Session(get_engine()) as session:
        row = _row(session, key)
        if fields.get('status') and fields['status'] not in ACTIVE:
            fields['items'] = _end_preparation(fields.get('items', json.loads(row.items)))
        for name, value in fields.items():
            setattr(row, name, _json(value) if name in {'scope', 'plan', 'items', 'requests'} else value)
        row.updated_at = now()
        session.add(row)
        session.commit()


SOURCE_DATE_WARNING = '部分原视频发布日期仍无法获取，请核对合集组内顺序；不会使用导入时间代替。'


def _source_date(uid):
    from dubforge import source_dates
    return source_dates.read(uid)


def _candidate(uid, selected_publication_id='', *, new_publication=False):
    project = crud.get_project(uid)
    if not project:
        raise ReleaseBatchError('所选项目不存在', code='PROJECT_NOT_FOUND')
    with Session(get_engine()) as session:
        runs = session.exec(select(WorkflowRun).where(WorkflowRun.project_uid == uid)
                            .order_by(WorkflowRun.created_at.desc())).all()
        pubs = session.exec(select(Publication).where(Publication.project_uid == uid, Publication.deleted_at.is_(None))
                            .order_by(Publication.created_at.desc())).all()
        recycled = session.exec(select(Publication).where(Publication.project_uid == uid,
                                Publication.deleted_at.is_not(None))).all()
        if selected_publication_id:
            if any(p.id == selected_publication_id for p in recycled):
                raise ReleaseBatchError('所选草稿在回收站，请先恢复原记录；不会另建稿件', code='PUBLISH_DELETED')
            if not any(p.id == selected_publication_id for p in pubs):
                raise ReleaseBatchError('所选发布记录不属于当前项目，请重新选择', code='RELEASE_BATCH_PUBLICATION_INVALID')
            run = next((r for r in runs if r.publication_id == selected_publication_id), None)
            pub_id = selected_publication_id
        elif new_publication:
            run, pub_id = None, ''
        else:
            run = next((r for r in runs if r.publication_id), runs[0] if runs else None)
            pub_id = run.publication_id if run and run.publication_id else (pubs[0].id if len(pubs) == 1 else '')
        blockers = []
        if run and run.publication_id and any(p.id == run.publication_id for p in recycled):
            blockers.append('上次流程关联的草稿在回收站，请先恢复该记录；不会另建稿件')
        if pub_id and any(p.id == pub_id and p.archived_at and not json.loads(p.receipt).get('bvid') for p in pubs):
            blockers.append('所选发布记录已归档，请先恢复后再继续')
        if len(pubs) > 1 and not selected_publication_id and not new_publication:
            blockers.append('项目有多份发布记录，请在本批次明确选择要继续的稿件')
        if any(p.status in service.ACTIVE or p.id in service._running for p in pubs):
            blockers.append('项目有其他正在执行或收尾的发布记录')
        # 项目可能保留一条旧的结果不明历史记录。明确选择另一条尚未投稿的
        # 草稿表示本次要换绑该草稿，旧记录仍保留审计但不应阻塞新稿；只有
        # 未选择稿件或明确选择 unknown 原稿时才要求先核对回执。
        if (not selected_publication_id and any(p.status == 'unknown' for p in pubs)):
            blockers.append('项目有投稿结果待核对的记录，请先关联原回执')
        if project.archived_at:
            blockers.append('项目已归档，请先恢复')
        if any(r.status in workflows.BUSY or r.id in workflows._running for r in runs):
            blockers.append('项目完整流程正在执行或收尾')
        if any(s.status in {'queued', 'running'} for s in crud.get_steps(project.id)):
            blockers.append('项目制作步骤正在执行或收尾')
        pub = service.view(pub_id) if pub_id else None
        if pub and (pub['status'] in service.ACTIVE or pub_id in service._running):
            blockers.append('稿件有正在执行或收尾的发布操作')
        if pub and pub['status'] == 'unknown':
            blockers.append('投稿结果待核对，请先在发布页关联原回执；禁止重复投稿')
        if pub and pub['status'] == 'rejected':
            blockers.append('稿件审核未通过，请先处理原稿件')
        choices = []
        for record in pubs:
            draft, snapshot, receipt = json.loads(record.draft), json.loads(record.snapshot), json.loads(record.receipt)
            title = (snapshot.get('settings') or {}).get('title') or (draft.get('overrides') or {}).get('title') or project.title
            created = record.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=datetime.timezone.utc)
            choices.append(dict(id=record.id, title=title, status=record.status, bvid=receipt.get('bvid', ''),
                                is_review_revision=isinstance(draft.get('review_source'), dict),
                                created_at=created.astimezone(datetime.timezone.utc).isoformat()))
        settings = configuration.resolve(uid)['values']
        versions = [{key: value[key] for key in ('id', 'name', 'dubbing', 'burn_subtitles', 'include_comments')}
                    for value in settings['assembly.versions']]
        selected_versions = settings['publish.version_ids']
        action = ('reuse_existing' if pub and pub['receipt'].get('bvid') else
                  'prepare' if not pub or pub['status'] == 'draft' else 'continue')
        return dict(project_uid=uid, title=project.title, source_date=_source_date(uid),
                    workflow_id=run.id if run else '', publication_id=pub_id,
                    status=pub['status'] if pub else run.status if run else 'queued',
                    phase=run.phase if run else '尚未开始', error=pub['error'] if pub else run.error if run else '',
                    blockers=blockers, publication_choices=choices, versions=versions,
                    selected_version_ids=selected_versions if selected_versions is not None else [v['id'] for v in versions],
                    publication_action=action)


def candidates(uids, publication_ids=None, *, target_driven=False):
    selected = {} if publication_ids is None else publication_ids
    if (not isinstance(selected, dict) or set(selected) - set(_uids(uids))
            or any(not isinstance(v, str) or not v or len(v) > 200 for v in selected.values())):
        raise ReleaseBatchError('所选稿件格式无效，请重新选择')
    if target_driven:
        if selected:
            raise ReleaseBatchError('顺序发布使用各项目已保存的原稿目标，请到发布步骤修改。')
        from dubforge import release_batch_targets
        rows = [release_batch_targets.candidate(uid) for uid in _uids(uids)]
    else:
        rows = [_candidate(uid, selected.get(uid, '')) for uid in _uids(uids)]
    warnings = [SOURCE_DATE_WARNING] if len(rows) > 1 and any(not r['source_date'] for r in rows) else []
    return dict(items=rows, warnings=warnings,
                **({'target_driven': True, 'derived_scope': rows[0]['saved_scope']} if target_driven else {}))


def _selection_map(raw, uids, *, versions=False):
    value = {} if raw is None else raw
    if not isinstance(value, dict) or set(value) - set(uids):
        raise ReleaseBatchError('版本或稿件操作必须属于本批次项目')
    for selected in value.values():
        if versions:
            if (not isinstance(selected, list) or not selected or len(selected) > 100
                    or any(not isinstance(key, str) or not key for key in selected)
                    or len(set(selected)) != len(selected)):
                raise ReleaseBatchError('请为每个项目选择至少一个不重复的成片版本')
        elif not isinstance(selected, str) or selected not in {'prepare', 'continue', 'reuse_existing'}:
            raise ReleaseBatchError('请选择有效的稿件处理方式')
    return value


def _collection_contains(collection, receipt):
    """小节归属由完整远端成员证明，历史上传配置不能证明当前归属。"""
    if not collection or not receipt.get('bvid'):
        return False
    aid, bvid = receipt.get('aid'), receipt['bvid']
    if type(aid) is not int or aid <= 0:
        raise ReleaseBatchError('原稿件缺少有效稿件编号，请先在发布页核对原BV回执',
                                code='RELEASE_BATCH_PUBLICATION_INVALID')
    matches = [e for e in collection['episodes'] if e['aid'] == aid or e['bvid'] == bvid]
    if matches and (len(matches) != 1 or matches[0]['aid'] != aid or matches[0]['bvid'] not in {'', bvid}):
        raise ReleaseBatchError('目标小节中的稿件编号与原BV不一致，请核对原回执；不会替换或重复投稿',
                                code='RELEASE_BATCH_PUBLICATION_INVALID')
    return bool(matches)


def _collection_cleanup_candidates(collection, selected_uids=()):
    """只读识别目标小节中的重复来源成员，供预检提示人工清理。

    合集接口只返回稿件身份，无法直接知道两个 BV 是否来自同一条原视频；
    本地发布快照同时保留项目 UID 和来源地址，因此把同项目的多个稿件、
    或来源身份相同的不同项目合并成一组。这里故意不调用远端写接口，也
    不自动移除任何成员，避免把审核退回稿件或用户手工加入的成员静默删除。
    """
    if not collection or not isinstance(collection.get('episodes'), list):
        return []
    episodes = collection['episodes']
    by_identity = {}
    for index, episode in enumerate(episodes):
        if not isinstance(episode, dict):
            continue
        aid, bvid = episode.get('aid'), episode.get('bvid')
        if type(aid) is not int and not isinstance(bvid, str):
            continue
        by_identity.setdefault(('aid', aid), []).append((index, episode)) if type(aid) is int else None
        if isinstance(bvid, str) and bvid:
            by_identity.setdefault(('bvid', bvid), []).append((index, episode))

    # 读取本地记录一次即可；合集通常只有几十项，避免在项目循环中 N+1 查询。
    with Session(get_engine()) as session:
        publications = session.exec(select(Publication).where(Publication.deleted_at.is_(None))).all()
        projects = {p.uid: p for p in session.exec(select(Project)).all()}

    matched = []
    for publication in publications:
        try:
            receipt = json.loads(publication.receipt) if publication.receipt else {}
        except (TypeError, ValueError):
            continue
        aid, bvid = receipt.get('aid'), receipt.get('bvid')
        matches = []
        if type(aid) is int:
            matches.extend(by_identity.get(('aid', aid), []))
        if isinstance(bvid, str) and bvid:
            matches.extend(by_identity.get(('bvid', bvid), []))
        # 一个本地回执同时命中 aid/bvid 只保留一次；不一致回执由
        # _collection_contains 在真正发布/排序前继续严格拦截。
        unique = {index: episode for index, episode in matches}
        for index, episode in unique.items():
            project = projects.get(publication.project_uid)
            source_identity = ''
            if project and project.source_url:
                try:
                    # 与自动命名/来源缓存共用同一套 YouTube canonical identity，
                    # 可识别 youtu.be 与 youtube.com/watch?v= 的等价地址。
                    from dubforge.project_titles import _source_identity
                    source_identity = _source_identity(project.source_url, project.source_kind)
                except (TypeError, ValueError):
                    source_identity = ''
            matched.append(dict(index=index, episode=dict(episode), publication_id=publication.id,
                                project_uid=publication.project_uid, project_title=project.title if project else '',
                                status=publication.status, bvid=bvid or episode.get('bvid', ''), aid=aid or episode.get('aid'),
                                source_identity=source_identity,
                                created_at=publication.created_at.isoformat() if publication.created_at else ''))
    if len(matched) < 2:
        return []

    # Union by project UID and canonical source identity so two projects imported
    # from the same URL are still reported as one duplicate group.
    parent = {}
    def find(value):
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value
    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left
    for row in matched:
        uid_key = ('project', row['project_uid'])
        union(uid_key, uid_key)
        if row['source_identity']:
            union(uid_key, ('source', row['source_identity']))
    groups = {}
    for row in matched:
        groups.setdefault(find(('project', row['project_uid'])), []).append(row)
    selected = set(selected_uids or ())
    selected_sources = {r['source_identity'] for r in matched
                        if r['project_uid'] in selected and r['source_identity']}
    result = []
    status_rank = {'published': 3, 'private': 3, 'reviewed': 3, 'submitted': 2,
                   'unknown': 1, 'rejected': 0}
    for rows in groups.values():
        # 同一 BV 被多个本地记录引用不是重复成员；按 aid/bvid 去重后再判断。
        unique_members = {(r['aid'], r['bvid']) for r in rows}
        if len(unique_members) < 2 or not (selected & {r['project_uid'] for r in rows}
                                           or selected_sources & {r['source_identity'] for r in rows if r['source_identity']}):
            continue
        rows.sort(key=lambda row: row['index'])
        keep = max(rows, key=lambda row: (status_rank.get(row['status'], -1), row['created_at'], row['index']))
        remove = [row for row in rows if row is not keep]
        result.append(dict(identity=keep['source_identity'] or 'project:' + keep['project_uid'],
                           candidates=rows, recommended_keep=keep,
                           recommended_remove=remove,
                           reason='同一来源存在多个合集成员；建议保留最新且审核通过的稿件，旧成员需人工确认后移出合集'))
    result.sort(key=lambda group: min(row['index'] for row in group['candidates']))
    return result


def _collection_cleanup_warnings(groups):
    warnings = []
    for group in groups or []:
        keep = group['recommended_keep']
        old = '、'.join(row.get('bvid') or str(row.get('aid')) for row in group['recommended_remove'])
        if old:
            warnings.append(f"发现同一来源在目标小节有重复成员：建议保留 {keep.get('bvid') or keep.get('aid')}，旧成员 {old} 不会自动移出；请在B站发布页明确确认后处理。")
    return warnings


def _publication_binding(pub, scope, *, collection=None, prepare=False):
    if prepare:
        if pub['receipt'].get('bvid') or pub['status'] != 'draft':
            raise ReleaseBatchError('仅未投稿且可编辑的草稿可以在制作后更新；已有BV须保留原稿或在发布页明确编辑',
                                    code='RELEASE_BATCH_PUBLICATION_INVALID')
        # 本次明确重绑新成片，旧草稿的空分P或失效修订不是制作前置条件。
        return dict(id=pub['id'], revision=pub['revision'], status=pub['status'],
                    fingerprint=digest(pub['draft']), bvid=None, aid=None,
                    collection_joined=False, reuse_existing=False, prepare=True)
    settings = pub.get('snapshot', {}).get('settings') or pub.get('settings') or {}
    account = pub.get('snapshot', {}).get('account_id') or pub.get('draft', {}).get('account_id')
    if account != scope['account_id']:
        raise ReleaseBatchError(f'原记录账号为 {account or "未设置"}，本批次账号为 {scope["account_id"]}；'
                                '请选择原投稿账号，已有BV不能转移到另一个账号',
                                code='RELEASE_BATCH_ACCOUNT_MISMATCH')
    if pub['status'] in {'unknown', 'rejected'}:
        raise ReleaseBatchError('原稿件结果需要核对或处理，不能自动重投')
    existing = _collection_contains(collection, pub['receipt'])
    if not existing and settings.get('season_id') != scope['season_id']:
        original = settings.get('season_id') or '未加入合集'
        guidance = ('目标小节尚未包含原BV，请在发布页“编辑原BV → 调整合集归属”处理后重新检查'
                    if pub['receipt'].get('bvid') else '请在发布页修改本草稿的合集设置后重新检查')
        raise ReleaseBatchError(f'原记录合集为 {original}，本批次合集为 {scope["season_id"]}'
                                f'（小节 {scope["section_id"]}）；{guidance}；不会另建稿件',
                                code='RELEASE_BATCH_COLLECTION_MISMATCH')
    if not existing and settings.get('section_id') != scope['section_id']:
        guidance = ('目标小节尚未包含原BV，请在发布页“编辑原BV → 调整合集归属”处理后重新检查'
                    if pub['receipt'].get('bvid') else '请在发布页修改本草稿的小节设置后重新检查')
        raise ReleaseBatchError(f'原记录小节为 {settings.get("section_id") or "未设置"}，'
                                f'本批次小节为 {scope["section_id"]}（合集 {scope["season_id"]}）；'
                                f'{guidance}；不会另建稿件', code='RELEASE_BATCH_SECTION_MISMATCH')
    # 已投稿回执不重做本地文件预检；待投稿资料则绑定原稿件的完整文件指纹。
    fingerprint = None if pub['receipt'].get('bvid') else service.plan(pub['id'])['fingerprint']
    return dict(id=pub['id'], revision=pub['revision'], fingerprint=fingerprint,
                bvid=pub['receipt'].get('bvid'), aid=pub['receipt'].get('aid'),
                collection_joined=existing or pub['receipt'].get('collection_joined') is True,
                reuse_existing=existing)


def _item_plan(row, scope, *, online=True, saved=None, collection=None, version_ids=None, publication_action=None, dubbing=None):
    if (row.get('target_recipe') or (saved or {}).get('recipe') or {}).get('target_driven'):
        from dubforge.release_batch_targets import item_plan
        return item_plan(row, scope, online=online, saved=saved, collection=collection)
    uid = row['project_uid']
    spec = configuration.run_spec(uid, mode='publish')
    saved_policy = (saved or {}).get('recipe', {}).get('dubbing')
    if dubbing is not None or saved_policy is not None:
        from dubforge.pipeline.dubbing_policy import current
        spec['dubbing'] = current(dubbing if dubbing is not None else saved_policy)
    chosen = version_ids if version_ids is not None else ((saved or {}).get('selected_version_ids') or row['selected_version_ids'])
    if set(chosen) - {version['id'] for version in row['versions']}:
        raise ReleaseBatchError('所选成片版本已移除，请重新选择', code='ASSEMBLY_VERSION_CONFLICT')
    spec['version_ids'] = chosen
    spec['part_titles'] = {key: title for key, title in spec['part_titles'].items() if key in chosen}
    action = publication_action or (saved or {}).get('publication_action') or row['publication_action']
    spec['account_id'] = scope['account_id']
    spec['overrides'] = {**spec.get('overrides', {}), 'season_id': scope['season_id'], 'section_id': scope['section_id']}
    blockers = list(row['blockers'])
    pub_binding = None
    if row['publication_id']:
        try:
            pub = service.view(row['publication_id'])
            # 上传已经开始后的恢复继续原上传快照，不改动已上传文件。
            if saved and not publication_action and pub['status'] != 'draft':
                action = row['publication_action']
            pub_binding = _publication_binding(pub, scope, collection=collection, prepare=action == 'prepare')
            if action == 'reuse_existing' and not pub['receipt'].get('bvid'):
                raise ReleaseBatchError('此记录尚未投稿，不能作为原BV复用')
            if pub['receipt'].get('bvid') and action != 'reuse_existing':
                raise ReleaseBatchError('已有BV仅核对与排序；请到发布页明确编辑原BV，批次不会替换或另投',
                                        code='RELEASE_BATCH_PUBLICATION_INVALID')
            if action == 'prepare':
                # 保留用户在草稿中明确编辑的文案；本批次目标账号及合集最终优先。
                spec['overrides'] = {**spec['overrides'], **pub['draft'].get('overrides', {}),
                                     'season_id': scope['season_id'], 'section_id': scope['section_id']}
                if pub['draft'].get('cover_id'):
                    spec.update(cover_source='custom', cover_id=pub['draft']['cover_id'])
            # 文件已绑定原稿件，制作依赖/新封面不影响继续原发布记录。
        except DubForgeError as exc:
            blockers.append(exc.message)
        if action == 'prepare':
            candidate = workflows.prepared_plan(uid, spec, online=online, dubbing_operation_id=row.get('workflow_id'))
            candidate['refresh_publication'] = row['publication_id']
            candidate['publication_binding'] = pub_binding
            blockers.extend(candidate['blockers'])
        else:
            # 原稿件已上传或有BV：仅继续该记录，绝不另建重复稿件。
            spec = copy.deepcopy(saved.get('recipe', spec)) if saved else spec
            spec['mode'] = 'publish'
            candidate = dict(recipe=spec, steps=[], labels={}, blockers=[], warnings=[], settings={}, origins={},
                         configuration='', fingerprint=digest(dict(publication=pub_binding, scope=_scope_identity(scope))),
                         recovery=dict(rows=[], start_label='复用原BV，仅核对审核与合集顺序'
                                       if pub_binding and pub_binding['reuse_existing'] else '继续原发布记录'))
    else:
        if action != 'prepare':
            blockers.append('项目没有发布记录，请选择制作并发布')
        candidate = workflows.prepared_plan(uid, spec, online=online, dubbing_operation_id=row.get('workflow_id'))
        blockers.extend(candidate['blockers'])
    if row['workflow_id'] and (not row['publication_id'] or action == 'prepare'):
        previous = workflows.get(row['workflow_id'])
        candidate, _ = workflows._recovery_plan(previous, candidate)
    elif not row['publication_id'] or action == 'prepare':
        from dubforge.pipeline.recovery import describe
        candidate['recovery'] = describe(workflows._plan_dependencies(crud.get_project(uid), candidate), candidate['steps'])
        from dubforge.pipeline import dubbing_policy
        dubbing_policy.apply_recovery(candidate.get('dubbing'), candidate['recovery'])
    return {**row, 'recipe': candidate['recipe'], 'plan': candidate, 'publication': pub_binding,
            'selected_version_ids': candidate['recipe']['version_ids'], 'publication_action': action,
            'production': candidate.get('recovery', {}),
            'blockers': list(dict.fromkeys(blockers)), 'status': saved['status'] if saved else 'queued',
            'error': saved.get('error', '') if saved else '',
            'phase': saved['phase'] if saved else '等待前项完成'}


def _fingerprint(result):
    return digest(dict(scope=_scope_identity(result['scope']), collection=result['collection']['fingerprint'],
        binding_collection=(result.get('binding_collection') or result['collection'])['fingerprint'],
        collection_cleanup=result.get('collection_cleanup') or [],
        items=[dict(project_uid=i['project_uid'], source_date=i['source_date'], recipe=i['recipe'],
                    plan_fingerprint=i['plan']['fingerprint'], recovery=i['plan'].get('recovery'),
                    workflow_id=i['workflow_id'], publication=i.get('publication'),
                    publication_action=i.get('publication_action'), selected_version_ids=i.get('selected_version_ids')) for i in result['items']]))


def preview(body, *, online=True):
    if not isinstance(body, dict):
        raise ReleaseBatchError('批次参数无效')
    uids = _uids(body.get('project_uids'))
    target_driven = body.get('target_driven') is True
    if target_driven:
        if any(key in body for key in ('scope', 'version_ids', 'publication_ids', 'publication_actions', 'dubbing')):
            raise ReleaseBatchError('顺序发布不再覆盖项目的账号、合集、版本或投稿方式，请在步骤页保存后重新检查。')
        from dubforge.release_batch_targets import saved_scope
        scope = _scope(saved_scope(uids[0]))
    else:
        scope = _scope(body.get('scope'))
    scope_display = _scope_display(body.get('scope_display'))
    versions = _selection_map(body.get('version_ids'), uids, versions=True)
    actions = _selection_map(body.get('publication_actions'), uids)
    rows = candidates(uids, body.get('publication_ids'), target_driven=target_driven)
    collection = service.read_collection_order(**_remote_scope(scope))
    items = [_item_plan(row, scope, online=online, collection=collection,
                        version_ids=versions.get(row['project_uid']), publication_action=actions.get(row['project_uid']),
                        dubbing=body.get('dubbing')) for row in rows['items']]
    collection_cleanup = _collection_cleanup_candidates(collection, {item['project_uid'] for item in items})
    blockers = [f'{item["title"]}：{reason}' for item in items for reason in item['blockers']]
    if body.get('order_confirmed') is not True:
        blockers.append('请核对并确认当前项目排列顺序')
    result = dict(scope=scope, scope_display=scope_display, items=items, collection=collection,
                  binding_collection=collection, blockers=blockers,
                  warnings=[*rows['warnings'], '制作与投稿按此顺序逐个执行；上一项的合集归属确认后才继续。',
                            '最终保留合集其他成员的相对顺序，将本批次按确认顺序排列在小节末尾。',
                            '遇到失败或不明回执暂停，程序重启后不会自动投稿。',
                            *_collection_cleanup_warnings(collection_cleanup)],
                  collection_cleanup=collection_cleanup)
    if target_driven:
        result['target_driven'] = True
    result['fingerprint'] = _fingerprint(result)
    return result


def _conflict(scope, uids, exclude=''):
    with Session(get_engine()) as session:
        for row in session.exec(select(ReleaseBatch).where(ReleaseBatch.status.in_(ACTIVE))).all():
            if row.id == exclude:
                continue
            other = _scope_identity(_scope(json.loads(row.scope)))
            members = [i['project_uid'] for i in json.loads(row.items)]
            if other == _scope_identity(scope) or set(members) & set(uids):
                raise ReleaseBatchError('已有批次占用相同合集小节或所选项目，请先暂停或停止', code='RELEASE_BATCH_BUSY')


def create(body):
    request = _request(body.get('request_id'))
    if body.get('confirmed') is not True:
        raise ReleaseBatchError('请确认项目顺序、发布范围与最终合集排列方式')
    with _lock, Session(get_engine()) as session:
        old = session.exec(select(ReleaseBatch).where(ReleaseBatch.request_id == request)).first()
        if old:
            if not _same_creation(old, body):
                raise ReleaseBatchError('同一请求标识不能创建不同方案', code='RELEASE_BATCH_CONFLICT')
            return _view(old)
    candidate = preview(body)
    if candidate['blockers'] or candidate['fingerprint'] != body.get('fingerprint'):
        raise ReleaseBatchError('预检未通过或已变化，请重新检查', code='RELEASE_BATCH_PLAN_CHANGED', detail=candidate['blockers'])
    with _lock, Session(get_engine()) as session:
        old = session.exec(select(ReleaseBatch).where(ReleaseBatch.request_id == request)).first()
        if old:
            if not _same_creation(old, body):
                raise ReleaseBatchError('同一请求标识不能创建不同方案', code='RELEASE_BATCH_CONFLICT')
            return _view(old)
        creation = dict(fingerprint=candidate['fingerprint'], scope=candidate['scope'], project_uids=body['project_uids'],
                        publication_ids={item['project_uid']: item.get('publication_id', '') for item in candidate['items']},
                        version_ids={item['project_uid']: item['selected_version_ids'] for item in candidate['items']},
                        publication_actions={item['project_uid']: item['publication_action'] for item in candidate['items']})
        if candidate.get('target_driven'):
            creation['target_driven'] = True
        if 'dubbing' in body:
            from dubforge.pipeline.dubbing_policy import normalize
            creation['dubbing'] = normalize(body['dubbing'])
        row = ReleaseBatch(request_id=request, scope=_json(candidate['scope']), plan=_json(candidate), items=_json(candidate['items']),
                           requests=_json({'_creation': creation}))
        session.add(row)
        session.commit()
        session.refresh(row)
        return _view(row)


def _same_creation(row, body):
    from dubforge.pipeline.dubbing_policy import normalize
    items = json.loads(row.items)
    receipt = json.loads(row.requests).get('_creation') or dict(fingerprint=json.loads(row.plan).get('fingerprint'),
        scope=json.loads(row.scope), project_uids=[item['project_uid'] for item in items],
        publication_ids={item['project_uid']: item.get('publication_id', '') for item in items})
    if receipt.get('target_driven') or body.get('target_driven'):
        return (receipt.get('target_driven') is True and body.get('target_driven') is True
                and body.get('fingerprint') == receipt['fingerprint']
                and body.get('project_uids') == receipt['project_uids']
                and not any(key in body for key in ('scope', 'version_ids', 'publication_ids', 'publication_actions', 'dubbing')))
    selected = body.get('publication_ids') or {}
    versions = body.get('version_ids') or {}
    actions = body.get('publication_actions') or {}
    return (normalize(receipt.get('dubbing')) == normalize(body.get('dubbing'))
            and receipt['fingerprint'] == body.get('fingerprint')
            and _scope_identity(_scope(body.get('scope'))) == _scope_identity(_scope(receipt['scope']))
            and body.get('project_uids') == receipt['project_uids']
            and isinstance(selected, dict)
            and isinstance(versions, dict) and isinstance(actions, dict)
            and not (set(versions) | set(actions)) - set(receipt['project_uids'])
            and all(versions.get(uid, receipt.get('version_ids', {}).get(uid)) == receipt.get('version_ids', {}).get(uid)
                    and actions.get(uid, receipt.get('publication_actions', {}).get(uid)) == receipt.get('publication_actions', {}).get(uid)
                    for uid in receipt['project_uids'])
            and all(selected.get(uid, receipt['publication_ids'][uid]) == receipt['publication_ids'][uid] for uid in receipt['project_uids'])
            and not set(selected) - set(receipt['project_uids']))


def create_prepared(candidate, request_id, parent_id):
    """混合批次内部登记冻结子批次；HTTP 不提供此入口，也不接受客户端配方。"""
    from dubforge.project_batches import get as parent_view
    parent = parent_view(parent_id)
    group = next((g for g in parent['groups'] if g['kind'] == 'collection'
                  and g['project_uids'] == [i['project_uid'] for i in candidate['items']]), None)
    if group is None or group['plan']['fingerprint'] != candidate['fingerprint'] or candidate['blockers']:
        raise ReleaseBatchError('混合批次子任务与已确认方案不一致', code='RELEASE_BATCH_OWNER')
    request_id = _request(request_id)
    with _lock, Session(get_engine()) as session:
        old = session.exec(select(ReleaseBatch).where(ReleaseBatch.request_id == request_id)).first()
        if old:
            if json.loads(old.plan).get('parent_batch_id') != parent_id:
                raise ReleaseBatchError('子任务请求标识已被占用', code='RELEASE_BATCH_CONFLICT')
            return _view(old)
        plan = {**copy.deepcopy(candidate), 'parent_batch_id': parent_id}
        row = ReleaseBatch(request_id=request_id, scope=_json(plan['scope']), plan=_json(plan), items=_json(plan['items']))
        session.add(row)
        session.commit()
        session.refresh(row)
        return _view(row)


def recovery_plan(key, *, online=True, collection=None, binding_collection=None, publication_ids=None):
    batch = get(key)
    if batch['status'] in ACTIVE:
        raise ReleaseBatchError('批次正在运行或停止中，请等待收尾')
    items = []
    blockers = []
    current_collection = (service.read_collection_order(**_remote_scope(batch['scope'])) if online else binding_collection
                          or collection or batch['plan'].get('binding_collection') or batch['plan']['collection'])
    selected_publications = publication_ids or {}
    if batch['plan'].get('target_driven') and selected_publications:
        raise ReleaseBatchError('此批次沿用已确认的项目稿件目标，请停止后到步骤页修改并创建新批次。')
    if not isinstance(selected_publications, dict) or set(selected_publications) - {item['project_uid'] for item in batch['items']}:
        raise ReleaseBatchError('恢复方案中的稿件选择不属于当前批次', code='RELEASE_BATCH_PUBLICATION_INVALID')
    for item in batch['items']:
        if item['status'] == 'done':
            items.append(copy.deepcopy(item))
            continue
        stored_publication_id = item.get('publication_id', '')
        selected_publication_id = selected_publications.get(item['project_uid'], stored_publication_id)
        run_publication_id = ''
        if item.get('workflow_id'):
            run_publication_id = (workflows.get(item['workflow_id']) or {}).get('publication_id', '')
        if not stored_publication_id and run_publication_id and item['project_uid'] not in selected_publications:
            # 第一次启动时还没有草稿ID；制作结束后该工作流自己绑定新稿件，
            # 这是同一任务的回执推进，不能误判成用户要求换绑到空草稿。
            stored_publication_id = selected_publication_id = run_publication_id
        fresh_target = bool(batch['plan'].get('target_driven') and not item.get('workflow_id')
            and not selected_publication_id and item.get('recipe', {}).get('mode') != 'update')
        row = _candidate(item['project_uid'], selected_publication_id, new_publication=fresh_target)
        # 启动请求已写入新草稿但流程行尚未切换时，数据库里会暂时出现“批次稿件”和
        #“流程稿件”不一致；仍按流程行的退回原稿进行一次显式换绑，不能被旧ID覆盖。
        previous_publication_id = run_publication_id if run_publication_id and run_publication_id != selected_publication_id else stored_publication_id
        rebound = selected_publication_id != previous_publication_id
        if rebound:
            # 换绑必须是用户在恢复页明确选择的草稿；只允许从退回/不明原稿切换，
            # 避免把已上传或已确认的稿件静默改成另一条记录。
            previous = service.view(previous_publication_id) if previous_publication_id else None
            replacement = service.view(selected_publication_id) if selected_publication_id else None
            if not previous or previous['status'] not in {'rejected', 'unknown'}:
                raise ReleaseBatchError('只有审核退回或结果不明的原稿件可以换绑草稿',
                                        code='RELEASE_BATCH_PUBLICATION_INVALID')
            if not replacement or replacement['status'] != 'draft' or replacement['receipt'].get('bvid'):
                raise ReleaseBatchError('换绑目标必须是尚未投稿且可编辑的草稿',
                                        code='RELEASE_BATCH_PUBLICATION_INVALID')
        # 未换绑时，创建后以批次已登记的同一流程/稿件为准；换绑时保留用户明确选择的草稿。
        if item.get('workflow_id'):
            run = workflows.get(item['workflow_id'])
            row['workflow_id'] = item['workflow_id']
            row['publication_id'] = selected_publication_id if rebound else (run['publication_id'] or stored_publication_id)
        elif selected_publication_id:
            row['publication_id'] = selected_publication_id
        row['publication_rebound'] = rebound
        current = _item_plan(row, batch['scope'], online=online, saved=item, collection=current_collection,
                             publication_action='prepare' if rebound else None)
        current.update(status='queued', phase='等待前项完成', error='')
        blockers.extend(f'{item["title"]}：{reason}' for reason in current['blockers'])
        items.append(current)
    collection_cleanup = _collection_cleanup_candidates(current_collection,
                                                        {item['project_uid'] for item in batch['items']})
    result = dict(scope=batch['scope'], scope_display=batch.get('scope_display') or batch['plan'].get('scope_display') or {},
                  items=items, collection=collection or batch['plan']['collection'],
                  binding_collection=current_collection, blockers=blockers,
                  warnings=[*batch['plan'].get('warnings', []), *_collection_cleanup_warnings(collection_cleanup)],
                  collection_cleanup=collection_cleanup, order_started=batch['order_started'])
    if batch['plan'].get('target_driven'):
        result['target_driven'] = True
    if batch['plan'].get('parent_batch_id'):
        result['parent_batch_id'] = batch['plan']['parent_batch_id']
    if batch['order_started'] and online:
        checked = service.verify_collection_order(**_remote_scope(batch['scope']), order=_batch_aids(batch), snapshot=batch['plan']['collection'])
        if checked.get('verified') is not True:
            result['blockers'].append('上次合集排序结果未确认，请先在B站核对并调整到预期顺序；不会自动重复发送排序请求')
    elif online:
        try:
            _check_collection_base(batch['plan']['collection'], current_collection, items)
        except ReleaseBatchError:
            # 没有发过排序时可提出新的完整基线，但须新指纹和用户重新确认后才落盘。
            result['collection'] = current_collection
            result['warnings'].append('合集成员、资料或顺序已变化：本次按当前其他成员的相对顺序保留，将本批次排列在小节末尾；请重新确认。')
    result['fingerprint'] = _fingerprint(result)
    return result


def start(key, body):
    global _closing
    request = _request(body.get('request_id'))
    if body.get('confirmed') is not True or body.get('rights_confirmed') is not True:
        raise ReleaseBatchError('请确认本次批次的顺序、发布范围及相应发布权利')
    publication_ids = body.get('publication_ids') or {}
    signature = digest(dict(fingerprint=body.get('fingerprint'), publication_ids=publication_ids,
                            confirmed=True, rights_confirmed=True))
    with _lock, Session(get_engine()) as session:
        row = _row(session, key)
        requests = json.loads(row.requests)
        if request in requests:
            if requests[request] != signature:
                raise ReleaseBatchError('同一启动标识不能确认不同方案', code='RELEASE_BATCH_CONFLICT')
            return _view(row)
        if _closing:
            raise ReleaseBatchError('服务正在关闭，请重启后重新确认')
        if row.status not in {'ready', 'paused', 'attention', 'stopped'}:
            raise ReleaseBatchError('当前批次不能启动或恢复')
    candidate = recovery_plan(key, publication_ids=publication_ids)
    if candidate['blockers'] or body.get('fingerprint') != candidate['fingerprint']:
        raise ReleaseBatchError('恢复预检已变化或未通过，请重新检查', code='RELEASE_BATCH_PLAN_CHANGED', detail=candidate['blockers'])
    from dubforge.runner import runner
    from dubforge.pipeline import dubbing_session
    from dubforge import runtime_edit
    with service._lock, runner._start_lock, dubbing_session.gate, video_info.gate, _lock:
        batch = get(key)
        with Session(get_engine()) as session:
            row = _row(session, key)
            requests = json.loads(row.requests)
            if request in requests:
                if requests[request] != signature:
                    raise ReleaseBatchError('同一启动标识不能确认不同方案', code='RELEASE_BATCH_CONFLICT')
                return _view(row)
        if batch['status'] not in {'ready', 'paused', 'attention', 'stopped'} or key in _running:
            raise ReleaseBatchError('批次状态已变化，请刷新')
        if batch['plan'].get('parent_batch_id'):
            from dubforge.project_batches import assert_child_start
            assert_child_start(batch['plan']['parent_batch_id'],key)
        _conflict(batch['scope'], [i['project_uid'] for i in batch['items']], key)
        for item in candidate['items']:
            if item['status'] != 'done':
                runtime_edit.check_idle(item['project_uid'])
        current = recovery_plan(key, online=False, collection=candidate['collection'],
                                binding_collection=candidate['binding_collection'],
                                publication_ids=publication_ids)
        if current['fingerprint'] != candidate['fingerprint'] or current['blockers']:
            raise ReleaseBatchError('预检期间设置、文件或记录发生变化，请重新确认', code='RELEASE_BATCH_PLAN_CHANGED')
        requests[request] = signature
        first = next((item for item in candidate['items'] if item.get('status') != 'done'), None)
        if first is not None:
            first.update(feedback='preparing', phase='正在核对已有结果与执行条件')
        _update(key, plan=candidate, items=candidate['items'], requests=requests, status='running', error='', phase='按确认顺序执行')
        event = threading.Event()
        _running[key] = event
        for item in candidate['items']:
            if item['status'] != 'done':
                _leases[item['project_uid']] = key
                if item.get('workflow_id'):
                    _children[item['project_uid']] = item['workflow_id']
        try:
            if candidate.get('parent_batch_id'):
                # 审核等待不能占满旧批次线程池，让第三个无关合集一起等待前两组。
                # 真正制作、编码和上传仍经过原有执行器的并发准入。
                threading.Thread(target=_execute,args=(key,event),daemon=True,
                                 name='mixed-release-' + key[:8]).start()
            else:
                _pool.submit(_execute, key, event)
        except Exception as exc:
            # 入队失败不能留下数据库 running 状态或内存租约；否则项目会被
            # 永久锁定，且下一次恢复会误以为已经启动过投稿流程。
            _running.pop(key, None)
            for uid, lease in list(_leases.items()):
                if lease == key:
                    _leases.pop(uid, None)
                    _children.pop(uid, None)
            message = '顺序发布任务未能加入后台队列，请重新检查并确认'
            _update(key, status='attention', error=message, phase='后台队列不可用，等待重新确认')
            raise ReleaseBatchError(message, code='RELEASE_BATCH_QUEUE_UNAVAILABLE') from exc
    return get(key)


def pause(key):
    with _lock:
        batch = get(key)
        if batch['status'] == 'running':
            _update(key, status='pausing', phase='当前项目处理完成后暂停，不再启动下一项')
        elif batch['status'] == 'ready':
            _update(key, status='paused', phase='批次已暂停')
    return get(key)


def stop(key):
    child_key = ''
    with _lock:
        batch = get(key)
        if event := _running.get(key):
            event.set()
            _update(key, status='stopping', phase='停止当前任务，等待已发送操作的回执')
            item = batch['items'][batch['cursor']] if batch['cursor'] < len(batch['items']) else None
            if item:
                child_key = item.get('workflow_id') or _children.get(item['project_uid'], '')
        elif batch['status'] != 'done':
            _update(key, status='stopped', phase='批次已停止，已有产物与稿件保留')
    if child_key:
        workflows.stop(child_key)
    return get(key)


def _cancel(key, event):
    if event.is_set() or _closing:
        raise ReleaseBatchError('批次已停止，已完成结果保留；不会自动继续投稿', code='RELEASE_BATCH_STOPPED')


def _save_item(key, index, **fields):
    with _lock:
        batch = get(key)
        items = batch['items']
        items[index].update(fields)
        _update(key, items=items)


def _wait_item(key, index, event):
    batch = get(key)
    item = batch['items'][index]
    workflow_id = item['workflow_id']
    while workflow_id in workflows._running:
        if event.is_set():
            workflows.stop(workflow_id)
        time.sleep(.1)
    _cancel(key, event)
    run = workflows.get(workflow_id)
    if run.get('mode') == 'update':
        # 原稿的旧审核/合集回执不能冒充本次替换与同步已经完成。
        while run['status'] in {'monitoring', 'running', 'queued'}:
            _cancel(key, event)
            task = workflows._update_task(run)
            if task:
                workflows._sync_update_task(workflow_id, task)
            run = workflows.get(workflow_id)
            if run['status'] not in {'monitoring', 'running', 'queued'}:
                break
            event.wait(.5)
        if run['status'] != 'completed':
            raise ReleaseBatchError(run['error'] or '原稿更新尚未完成，请查看并处理本次更新任务')
        _save_item(key, index, publication_id=run['publication_id'], phase='本次原稿更新已完成，核对合集归属', status='monitoring')
        _verified_item(key, index, service.view(run['publication_id']), event)
        return
    if not run['publication_id']:
        raise ReleaseBatchError(run['error'] or '项目制作未完成，请查看步骤日志')
    _save_item(key, index, publication_id=run['publication_id'], phase='等待合集归属确认', status='monitoring')
    pub = service.view(run['publication_id'])
    last_status_refresh = 0.0
    while run['status'] == 'monitoring':
        _cancel(key, event)
        if pub['status'] in {'rejected', 'unknown', 'collection_pending'}:
            break
        if pub['status'] in {'published', 'private', 'reviewed'} and pub['receipt'].get('collection_joined'):
            break
        # B站投稿接口在审核中会返回 submitted/-60；单次投稿线程随后结束，
        # 若批次只轮询本地记录就会永久等待。定时只读核对远端状态，严禁重新上传或投稿。
        if (pub['status'] == 'submitted' and pub['receipt'].get('bvid')
                and time.monotonic() - last_status_refresh >= 15):
            try:
                pub = service.refresh_status(pub['id'], cancel=event)
            except DubForgeError:
                # 平台暂时不可读时保留 submitted，下一轮继续核对；不把瞬时网络故障误判为失败。
                pass
            last_status_refresh = time.monotonic()
        event.wait(.5)
        run = workflows.get(workflow_id)
        pub = service.view(run['publication_id'])
    if run['status'] in {'failed', 'paused', 'attention'}:
        raise ReleaseBatchError(run['error'] or '审核或合集状态需要处理，请查看原稿件')
    if pub['status'] == 'unknown':
        raise ReleaseBatchError('投稿回执待核对，已暂停；不会重复投稿')
    if not pub['receipt'].get('bvid') or not pub['receipt'].get('aid'):
        raise ReleaseBatchError(pub['error'] or run['error'] or '投稿未完成，请继续原稿件')
    if pub['status'] not in {'published', 'private', 'reviewed'} or not pub['receipt'].get('collection_joined'):
        raise ReleaseBatchError(pub['error'] or '视频已投稿，但合集尚未确认，请先在发布页处理原稿件')
    _verified_item(key, index, pub, event)


def _verified_item(key, index, pub, event, *, reused=False):
    """已投稿条目以真实稿件和小节成员身份收尾，不改历史上传快照。"""
    batch = get(key)
    # 加入回执之外再回读真实小节。短暂平台延迟有限等待；失败暂停，不重复加入。
    for attempt in range(3):
        _cancel(key, event)
        current = service.read_collection_order(**_remote_scope(batch['scope']), cancel=event)
        _cancel(key, event)
        if _collection_contains(current, pub['receipt']):
            binding = _publication_binding(pub, batch['scope'], collection=current)
            binding['reuse_existing'] = reused
            _save_item(key, index, status='done', phase='原BV审核与目标小节已核对' if reused else '已投稿且合集归属已确认', error='',
                       publication=binding)
            return
        if attempt < 2:
            event.wait(2)
    raise ReleaseBatchError('平台尚未确认本项合集归属，批次已暂停；请稍后核对原稿件')


def _review_existing(key, receipt, event):
    """只核对原BV当前审核，不修改已完成条目或历史发布快照。"""
    batch = get(key)
    _cancel(key, event)
    remote = studio.read(batch['scope']['account_id'], 'detail', {'bvid': receipt['bvid']}, cancel=event)
    _cancel(key, event)
    if (remote.get('mid') != batch['scope']['account_id'] or remote.get('bvid') != receipt['bvid']
            or type(remote.get('aid')) is not int or remote.get('aid') != receipt['aid']):
        raise ReleaseBatchError('远端原BV归属或稿件编号与原回执不一致，请核对；不会替换或重复投稿',
                                code='RELEASE_BATCH_PUBLICATION_INVALID')
    state = remote.get('state')
    # 与现有发布状态机一致：0代表审核通过，私密稿件也可核对归属；文字说明只用于展示。
    if type(state) is not int or state != 0:
        reason = str(remote.get('reject_reason') or remote.get('state_desc') or '平台未提供说明')[:300]
        message = '原BV当前审核未通过' if type(state) is int and state in {-2, -16} else '原BV当前审核尚未确认通过'
        raise ReleaseBatchError(f'{message}（状态 {state}）：{reason}；批次已暂停，请处理或等待后重新检查继续',
                                code='RELEASE_BATCH_REVIEW_PENDING')


def _verify_existing(key, index, pub, event):
    """复用远端原BV只查当前审核，不恢复旧流程、不上传或加入旧合集。"""
    _save_item(key, index, status='monitoring', phase='核对原BV当前审核与目标小节', error='')
    _review_existing(key, pub['receipt'], event)
    _verified_item(key, index, pub, event, reused=True)


def _expected_order(batch, current):
    # 运行期间旧成员的相对顺序也属于确认快照。只校验成员集合会让
    # 外部重排在最终排序时被悄悄覆盖，导致用户无法发现并发修改。
    _check_collection_base(batch['plan']['collection'], current, batch['items'])
    aids = _batch_aids(batch)
    if any(not _collection_contains(current, item['publication']) for item in batch['items']):
        raise ReleaseBatchError('批次原BV已不在目标小节中，请重新核对；不会自动移回或重复投稿')
    original = batch['plan']['collection']['episodes']
    old = [e['aid'] for e in original if e['aid'] not in aids]
    if set(e['aid'] for e in current['episodes']) != set(old + aids):
        raise ReleaseBatchError('合集成员在运行期间发生变化，请核对；不会覆盖外部修改')
    return old + aids


def _batch_aids(batch):
    aids = []
    for item in batch['items']:
        binding = item.get('publication') or {}
        if not binding.get('aid') and item.get('publication_id'):
            binding = service.view(item['publication_id'])['receipt']
        if type(binding.get('aid')) is not int or binding['aid'] <= 0:
            raise ReleaseBatchError('批次缺少已确认稿件回执，不能排列合集')
        aids.append(binding['aid'])
    if len(set(aids)) != len(aids):
        raise ReleaseBatchError('多个项目关联同一稿件，无法按项目顺序排列，请核对原记录')
    return aids


def _check_collection_base(initial, current, items):
    if initial['section'] != current['section']:
        raise ReleaseBatchError('合集小节资料发生变化，请重新核对', code='RELEASE_BATCH_PLAN_CHANGED')
    originals = {e['aid']: e for e in initial['episodes']}
    remotes = {e['aid']: e for e in current['episodes']}
    allowed = set(originals) | {i['publication']['aid'] for i in items if i.get('publication') and i['publication'].get('aid')}
    if not set(originals) <= set(remotes) or not set(remotes) <= allowed:
        raise ReleaseBatchError('合集成员发生外部变化，请核对；不会覆盖外部修改', code='RELEASE_BATCH_PLAN_CHANGED')
    if [e['aid'] for e in current['episodes'] if e['aid'] in originals] != list(originals):
        raise ReleaseBatchError('合集原有成员顺序发生变化，请核对', code='RELEASE_BATCH_PLAN_CHANGED')
    for aid, episode in originals.items():
        if any(episode.get(k) != remotes[aid].get(k) for k in ('id', 'aid', 'cid', 'bvid', 'title')):
            from dubforge.release_batch_targets import allows_episode_update
            if any((item.get('publication') or {}).get('aid') == aid and allows_episode_update(item, episode, remotes[aid])
                   for item in items):
                continue
            raise ReleaseBatchError('合集原有成员资料发生变化，请核对', code='RELEASE_BATCH_PLAN_CHANGED')


def _execute(key, event):
    index = None
    try:
        with _owned(key):
            batch = get(key)
            for index, item in enumerate(batch['items']):
                if item['status'] == 'done':
                    continue
                _cancel(key, event)
                if get(key)['status'] == 'pausing':
                    _update(key, status='paused', phase='已暂停，可重新预检后继续')
                    return
                _update(key, cursor=index, phase=f'第{index + 1}/{len(batch["items"])}项：{item["title"]}')
                _save_item(key, index, status='running', phase='正在核对已有结果与执行条件', error='', feedback='preparing')
                reused = False
                if item.get('publication_id') and item.get('publication_action') != 'update_existing':
                    pub = service.view(item['publication_id'])
                    current = service.read_collection_order(**_remote_scope(batch['scope']), cancel=event) if pub['receipt'].get('bvid') else batch['plan']['collection']
                    _cancel(key, event)
                    binding = _publication_binding(pub, batch['scope'], collection=current,
                                                   prepare=item.get('publication_action') == 'prepare')
                    if binding != item.get('publication'):
                        raise ReleaseBatchError('原稿件在批次确认后发生变化，请重新预检', code='RELEASE_BATCH_PLAN_CHANGED')
                    reused = binding['reuse_existing']
                if reused:
                    _save_item(key, index, feedback='')
                    _verify_existing(key, index, pub, event)
                else:
                    request_seed = 'dubforge:release:' + key + ':' + item['project_uid']
                    if item.get('publication_rebound'):
                        # 换绑后必须使用新的幂等键；沿用旧流程的 request_id 会命中旧稿件行，
                        # 被误判为“批次不能替换已有关联稿件”。
                        request_seed += ':rebind:' + str(item.get('publication_id') or '')
                    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, request_seed))
                    # 换绑到新草稿时不能复用已关联旧稿件的流程行；明确换绑已在恢复预检中确认，
                    # 此处创建新的流程行并保留旧流程作为历史记录。
                    existing_id = '' if item.get('publication_rebound') else item.get('workflow_id', '')
                    prepared = item['plan']
                    options = {}
                    if batch['plan'].get('parent_batch_id') and not existing_id and not item.get('publication_id'):
                        # 混合批次冻结目标，排队中的普通参数在消费边界重新解析。
                        prepared = workflows.prepared_plan(item['project_uid'], item['recipe'], online=False,
                                                            previous_update=item['plan'].get('update_plan'))
                        if prepared['blockers']:
                            raise ReleaseBatchError('；'.join(prepared['blockers']))
                        options['frozen_target'] = True
                    run = workflows.start_prepared(item['project_uid'], prepared, request_id,
                                                   existing_id=existing_id, publication_id=item.get('publication_id', ''), **options)
                    _save_item(key, index, workflow_id=run['id'], publication_id=run['publication_id'], feedback='')
                    _wait_item(key, index, event)
                _update(key, cursor=index + 1)
                # 每项结束后放开其制作资料；小节仍由整批占用。
                with _lock:
                    _leases.pop(item['project_uid'], None)
                    _children.pop(item['project_uid'], None)
            _cancel(key, event)
            if get(key)['status'] == 'pausing':
                _update(key, status='paused', phase='所有项目已投稿，最终合集排序尚未执行')
                return
            batch = get(key)
            _update(key, phase='核对并整理合集顺序')
            current = service.read_collection_order(**_remote_scope(batch['scope']), cancel=event)
            # 先验证成员集合与现有顺序仍符合本批次快照；真正发送的顺序
            # 由已确认的批次项目 aid 列表给出，不能把校验结果误当成发送参数。
            _expected_order(batch, current)
            if batch['order_started']:
                verified = service.verify_collection_order(**_remote_scope(batch['scope']), order=_batch_aids(batch), snapshot=batch['plan']['collection'])
                if verified.get('verified') is not True:
                    raise ReleaseBatchError('上次排序结果未确认，请先核对平台实际顺序；不会自动重发')
            else:
                # 暂停或制作后续项目期间原BV可能又被编辑，发送排序前重新确认当前审核。
                for item in batch['items']:
                    if item['publication'].get('reuse_existing'):
                        _review_existing(key, item['publication'], event)
                # 副作用发送前落盘，响应丢失或重启不会自动重复排序。
                with _lock:
                    _cancel(key, event)
                    if get(key)['status'] == 'pausing':
                        _update(key, status='paused', phase='已暂停，最终合集排序尚未执行')
                        return
                    _update(key, order_started=True)
                try:
                    service.apply_collection_order(**_remote_scope(batch['scope']), order=[i['publication']['aid'] for i in batch['items']],
                                                   snapshot=batch['plan']['collection'], confirmed=True, cancel=event)
                except DubForgeError as exc:
                    if exc.code in {'PUBLISH_COLLECTION_ORDER_CHANGED', 'PUBLISH_COLLECTION_ORDER_INVALID',
                                    'PUBLISH_COLLECTION_ORDER_REJECTED', 'PUBLISH_COLLECTION_ORDER_NOT_SENT'}:
                        # 明确未发送或明确业务拒绝，可在新预检确认后重试；不明回执仍冻结。
                        _update(key, order_started=False)
                    raise
            _update(key, status='done', cursor=len(batch['items']), phase='顺序发布完成，合集顺序已核对', error='')
    except Exception as exc:
        if not isinstance(exc,DubForgeError):
            logger.exception('顺序发布批次 %s 执行失败',key)
        message = exc.message if isinstance(exc, DubForgeError) else '顺序发布未完成，请检查当前项目和发布记录'
        if index is not None and index < len(get(key)['items']) and get(key)['items'][index]['status'] != 'done':
            _save_item(key, index, status='attention', error=message, phase='需要处理后重新确认', feedback='')
        _update(key, status='stopped' if event.is_set() or _closing else 'attention', error=message,
                phase='批次已停止，已有结果保留' if event.is_set() or _closing else '批次已暂停，等待处理')
    finally:
        with _lock:
            _running.pop(key, None)
            for uid in [uid for uid, lease in _leases.items() if lease == key]:
                _leases.pop(uid, None)
                _children.pop(uid, None)


def recover():
    global _closing
    with _lock, Session(get_engine()) as session:
        _closing = False
        for row in session.exec(select(ReleaseBatch).where(ReleaseBatch.status.in_(ACTIVE))).all():
            row.status = 'paused'
            row.phase = '程序重启后已暂停，请重新检查并确认'
            row.error = '重启不会自动上传、投稿或排序，已有产物与回执保留'
            row.items = _json([_item_display(item, 'paused') for item in _end_preparation(json.loads(row.items))])
            row.updated_at = now()
            session.add(row)
        session.commit()


def shutdown():
    global _closing
    with _lock:
        _closing = True
        for event in _running.values():
            event.set()
