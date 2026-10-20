"""原BV更新：一次确认，冻结文件与差异，按已确认阶段恢复，不另发新稿。"""
import copy
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlmodel import Session, select

from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.domain.models import Artifact
from dubforge.errors import DubForgeError
from dubforge.publishing import service as pub, studio, lifecycle, transport
from dubforge.publishing.models import Publication, PublicationUpdate, PublishChange, now


WRITING = {'queued', 'uploading', 'submitting'}
OPEN = WRITING | {'waiting', 'unknown', 'delivering'}
_running = {}
_lock = threading.RLock()
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='publication-update')
_stop = threading.Event()
_monitor = None
_owner = threading.local()
_STAGES = dict(upload='上传修订视频', edit='提交原稿更新', review='等待并核对审核',
               chapters='同步视频章节', collection='核对合集', verify='回读确认')


def _stage_progress(execution):
    """把原稿更新的阶段状态投影为稳定、可直接用于界面的进度快照。

    旧实现只按已结束阶段计数，上传一个大分P时会长期显示 0%，章节整理的
    LLM 进度也不会反映到总体进度。这里保留阶段原始字段，同时补齐统一的
    ``progress``（0--100）和当前阶段，供更新页、项目空间和总览复用。
    """
    raw = execution.get('stages') if isinstance(execution, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    stages = []
    for key, default_label in _STAGES.items():
        source = raw.get(key) if isinstance(raw.get(key), dict) else {}
        status = source.get('status', 'pending')
        if status in {'completed', 'skipped'}:
            progress = 100.0
        else:
            value = source.get('progress', 0)
            try:
                progress = float(value)
            except (TypeError, ValueError):
                progress = 0.0
            if not math.isfinite(progress):
                progress = 0.0
            progress = max(0.0, min(100.0, progress))
        item = copy.deepcopy(source)
        item.update(key=key, label=source.get('label') or default_label,
                    status=status, progress=round(progress, 1),
                    # 上传等阶段有可计量进度；审核/远端回读/模型处理通常只能
                    # 报告当前阶段，标记为不定长，避免伪造百分比。
                    indeterminate=(status in {'running', 'waiting', 'queued'}
                                   and not isinstance(source.get('progress'), (int, float))))
        stages.append(item)
    # 跳过的阶段不应把总体进度提前推高；这与前端阶段进度和用户对
    # “本次实际要做的工作”一致。若整项任务全部跳过，才回退到完整清单。
    considered = [item for item in stages if item['status'] != 'skipped'] or stages
    total = len(considered) or 1
    overall = round(sum(item['progress'] for item in considered) / total, 1)
    active = next((item for item in stages if item['status'] in {'running', 'waiting', 'queued'}), None)
    return dict(progress=overall, stages=stages,
                completed_stages=sum(item['status'] in {'completed', 'skipped'} for item in stages),
                total_stages=len(stages), effective_total_stages=len(considered),
                current_stage=active['key'] if active else None,
                current_stage_label=active['label'] if active else '',
                indeterminate=bool(active and active.get('indeterminate')),
                phase=execution.get('phase', ''), detail=execution.get('detail', ''))


def progress_summary(row):
    """面向总览/项目空间的轻量更新进度，不包含冻结方案和文件路径。"""
    try:
        execution = json.loads(row.execution or '{}')
    except (TypeError, ValueError):
        # 列表轮询不能因为一条旧的损坏快照整体 500；发布详情仍会保留
        # 原始记录并在用户打开时给出更具体的核对提示。
        execution = {}
    if not isinstance(execution, dict):
        execution = {}
    detail = _stage_progress(execution)
    return dict(id=row.id, status=row.status, phase=row.phase,
                progress=round(max(float(row.progress or 0), detail['progress']), 1),
                error=row.error, error_code=row.error_code,
                busy=row.id in _running, updated_at=row.updated_at.isoformat(),
                execution=detail)


def _error(message, code='PUBLISH_UPDATE_INVALID'):
    return pub.PublishError(message, code=code)


def current_owner():
    return getattr(_owner, 'key', '')


def _has_unknown(execution, session):
    if execution.get('edit_unverified'):
        return True
    delivery = execution.get('delivery', {})
    nodes = [*delivery.get('parts', []), delivery.get('collection', {})]
    identifiers = [execution.get('change_id'), *(node.get('change_id') for node in nodes)]
    if any(node.get('status') in {'unknown', 'committing'} for node in nodes):
        return True
    return any((change := session.get(PublishChange, key)) is not None
               and change.status in {'unknown', 'committing'} for key in identifiers if key)


def _assert_remote_free(session, account, bvid, execution=None):
    own = execution or {}
    delivery = own.get('delivery', {})
    identifiers = {own.get('change_id'), *(p.get('change_id') for p in delivery.get('parts', [])),
                   delivery.get('collection', {}).get('change_id')}
    if session.exec(select(PublishChange).where(PublishChange.account_id == account,
            PublishChange.bvid == bvid, PublishChange.status.in_({'committing', 'unknown'}),
            PublishChange.id.not_in([key for key in identifiers if key]))).first():
        raise _error('该原稿有其他结果待核对的修改，请先核对再更新视频', 'PUBLISH_WRITE_UNKNOWN')


def _proven_parts(source):
    """原上传与随后已核验更新共同构成当前绑定证据，不改写原始历史。"""
    bound = {}
    for part in source['snapshot'].get('parts', []):
        transfer = source['transfers'].get(str(part.get('artifact_id')), {})
        if type(transfer.get('cid')) is int and part.get('sha256'):
            bound[transfer['cid']] = (copy.deepcopy(part), {**transfer, 'sha256': part['sha256']})
    with Session(get_engine()) as session:
        history = session.exec(select(PublicationUpdate).where(
            PublicationUpdate.source_publication_id == source['id']).order_by(PublicationUpdate.created_at)).all()
    for update in history:
        execution = json.loads(update.execution)
        if execution.get('stages', {}).get('edit', {}).get('status') != 'completed':
            continue
        snapshot = json.loads(update.plan)
        final_ids = {p.get('cid') for p in execution.get('final_parts', [])}
        for part in snapshot.get('parts', []):
            transfer = execution.get('transfers', {}).get(str(part['artifact_id']), {})
            if transfer.get('cid') in final_ids and part.get('sha256'):
                bound[transfer['cid']] = (copy.deepcopy(part), {**transfer, 'sha256': part['sha256']})
    return bound


def superseding_update(source_id):
    with Session(get_engine()) as session:
        rows = session.exec(select(PublicationUpdate).where(
            PublicationUpdate.source_publication_id == source_id).order_by(PublicationUpdate.updated_at.desc())).all()
        for row in rows:
            execution = json.loads(row.execution)
            if (execution.get('stages', {}).get('edit', {}).get('status') == 'completed'
                    and any(part.get('replaced') for part in json.loads(row.plan).get('parts', []))):
                return _public(row)
    return None


def _get(key, session):
    row = session.get(PublicationUpdate, key)
    if row is None:
        raise _error('原稿更新任务不存在', 'PUBLISH_UPDATE_NOT_FOUND')
    return row


def _source(key):
    with Session(get_engine()) as session:
        row = pub._get(session, Publication, key)
        lifecycle.assert_active(row)
        if row.status not in pub.REMOTE_COPY_STATUSES:
            raise _error('请选择已有BV、状态已明确的原发布记录')
        snapshot, receipt = json.loads(row.snapshot), json.loads(row.receipt)
        bvid = studio._bvid(receipt.get('bvid'))
        account = snapshot.get('account_id')
        if not isinstance(account, str) or not account:
            raise _error('原稿账号记录不完整，请先核对原发布回执')
        pub._project(row.project_uid)
        return dict(id=row.id, revision=row.revision, project_uid=row.project_uid,
            account_id=account, account_name=snapshot.get('account_name', account), bvid=bvid,
            aid=receipt.get('aid'), title=snapshot.get('settings', {}).get('title', ''),
            snapshot=snapshot, receipt=receipt, transfers=json.loads(row.transfers))


def _source_view(source):
    return {key: source.get(key) for key in ('id', 'revision', 'project_uid', 'account_id',
                                            'account_name', 'bvid', 'aid', 'title')}


def _owned(source, remote):
    if (not isinstance(remote, dict) or remote.get('bvid') != source['bvid']
            or str(remote.get('mid')) != source['account_id']
            or not remote.get('aid') or not remote.get('version')):
        raise _error('无法核对原稿归属与远端版本，未准备更新', 'PUBLISH_REMOTE_OWNERSHIP')
    if source.get('aid') and source['aid'] != remote['aid']:
        raise _error('原稿编号与B站返回不一致，未准备更新', 'PUBLISH_REMOTE_CHANGED')
    parts = remote.get('videos')
    if (not isinstance(parts, list) or not parts or len(parts) > 100
            or any(type(p.get('cid')) is not int or p['cid'] <= 0 for p in parts)
            or len({p['cid'] for p in parts}) != len(parts)):
        raise _error('B站未返回可核验的完整分P清单', 'PUBLISH_RESPONSE_UNSUPPORTED')
    return remote


def _remote(source):
    return _owned(source, studio.read(source['account_id'], 'detail', {'bvid': source['bvid']}))


def _public(row):
    execution = json.loads(row.execution)
    detail = _stage_progress(execution)
    safe = {k: copy.deepcopy(execution[k]) for k in ('phase', 'outcome', 'next_check',
        'error', 'error_code', 'progress', 'current_part', 'loaded_bytes', 'total_bytes', 'receipt_sync_error') if k in execution}
    # 保留阶段原始字段（例如当前上传分P），同时确保每个阶段都有统一进度。
    # 兼容既有调用方：execution.stages 原来是以阶段键为索引的字典，
    # 不能为了新增进度字段改成数组。
    safe['stages'] = {item['key']: item for item in detail['stages']}
    safe['progress'] = detail['progress']
    safe['progress_detail'] = detail
    delivery = execution.get('delivery', {})
    if delivery:
        safe['delivery'] = dict(parts=[{k: p[k] for k in ('artifact_id', 'cid', 'title', 'status',
            'items', 'adaptation', 'error', 'error_code') if k in p} for p in delivery.get('parts', [])],
            collection={k: v for k, v in delivery.get('collection', {}).items() if k != 'change_id'})
        if delivery.get('execution'):
            safe['delivery']['execution'] = copy.deepcopy(delivery['execution'])
    return dict(id=row.id, source_publication_id=row.source_publication_id, project_uid=row.project_uid,
        bvid=row.bvid, account_id=row.account_id, status=row.status, phase=row.phase,
        delivery_mode=json.loads(row.plan).get('delivery_mode', 'publish'),
        fingerprint=row.fingerprint, preview=json.loads(row.preview), execution=safe,
        error=row.error, error_code=row.error_code,
        progress=round(max(float(row.progress or 0), detail['progress']), 1),
        busy=row.id in _running, next_check=execution.get('next_check', 0),
        created_at=row.created_at.isoformat(), updated_at=row.updated_at.isoformat())


def view(key):
    with Session(get_engine()) as session:
        return _public(_get(key, session))


def list_updates(source_id):
    with Session(get_engine()) as session:
        return [_public(row) for row in session.exec(select(PublicationUpdate).where(
            PublicationUpdate.source_publication_id == source_id).order_by(PublicationUpdate.updated_at.desc()).limit(20)).all()]


def assert_available(session, *, account='', bvid='', uid='', excluding='', writing_only=False):
    statuses = WRITING if writing_only else OPEN
    rows = session.exec(select(PublicationUpdate).where(PublicationUpdate.status.in_(statuses))).all()
    if any(row.id != excluding and ((account and row.account_id == account and row.bvid == bvid)
                                   or uid and row.project_uid == uid) for row in rows):
        raise _error('原稿更新正在执行或等待核对，请先继续、暂停或核对已有任务', 'PUBLISH_UPDATE_BUSY')


def _suggestions(source, remote, files):
    old = {cid: pair[0] for cid, pair in _proven_parts(source).items()}
    result = []
    for item in remote['videos']:
        original = old.get(item['cid'])
        match = []
        if original:
            # 只按明确版本/修订来源匹配，不能按同名文件或时长猜原分P。
            for candidate in files:
                if not candidate.get('exists') or candidate.get('review_ready') is False:
                    continue
                if (candidate.get('review_source_artifact_id') == original['artifact_id']
                        or candidate.get('version_id') and candidate.get('version_id') == original.get('version_id')):
                    match.append(candidate)
        result.append(dict(old_cid=item['cid'], artifact_id=match[0]['id'] if len(match) == 1 else None))
    return result


def context(key):
    source = _source(key)
    remote = _remote(source)
    available = pub.candidates(source['project_uid'])
    from dubforge.publishing.project_copy import preview
    project = preview(source['project_uid'])
    try:
        metadata = pub.metadata(source['account_id'])
    except DubForgeError:
        metadata = dict(seasons=[], collection_error=True)
    public_remote = copy.deepcopy(remote)
    known = _proven_parts(source)
    for item in public_remote['videos']:
        item.pop('filename', None)
        if not item.get('duration'):
            previous = known.get(item['cid'], ({}, {}))[0]
            item['duration'] = previous.get('duration')
            item['duration_source'] = '原上传记录' if item['duration'] else '未返回'
    return dict(source=_source_view(source), remote=public_remote,
        has_dubbing=any(part.get('has_dubbing') for part, _ in known.values()),
        candidates=[{k: v for k, v in p.items() if k != 'path'} for p in available['files']],
        suggested_replacements=_suggestions(source, remote, available['files']),
        project_values={k: v for k, v in project['values'].items() if k in studio.EDIT_FIELDS},
        chapter_options=source['snapshot'].get('chapter_options') or pub._chapter_options(
            dict(items=available.get('chapters', []))),
        metadata=metadata, capabilities=dict(field_capabilities=copy.deepcopy(pub.STUDIO_FIELD_CAPABILITIES),
            replace_parts=True), updates=list_updates(key))


def _bool_options(value, allowed):
    if not isinstance(value, dict) or set(value) - allowed:
        raise _error('更新范围参数无效')
    for key in {'enabled', 'translate', 'permanent', 'overwrite_existing'} & value.keys():
        if type(value[key]) is not bool:
            raise _error('更新范围开关必须明确选择')
    return value


def _freeze_file(source, item, old):
    project = pub._project(source['project_uid'])
    artifact_id = item.get('artifact_id')
    if type(artifact_id) is not int or artifact_id <= 0:
        raise _error('请选择项目中已生成的新成片')
    with Session(get_engine()) as session:
        artifact = pub._get(session, Artifact, artifact_id)
    path = pub._eligible_file(artifact, project, crud.project_dir_by_project(project).resolve())
    if not path or not path.is_file():
        raise _error('替换视频只能选择本项目已登记的有效视频')
    from dubforge.content_review_service import assert_publishable
    from dubforge.publishing.review_draft import assert_revision_current
    assert_publishable(project, artifact)
    assert_revision_current(project, artifact, verify_hash=True)
    if not 0 < path.stat().st_size <= 16 * 1024 ** 3 or pub._file_hash(path) != artifact.sha256:
        raise _error('新成片缺失、超过16GB或内容与登记不一致，请重新生成', 'PUBLISH_UPDATE_FILE_CHANGED')
    from dubforge.pipeline.assembly_versions import artifact_metadata, artifact_registered_at
    metadata = artifact_metadata(source['project_uid']).get(artifact_id, {})
    has_dubbing = metadata.get('has_dubbing', artifact.step_key == 'assemble'
                              and not artifact.name.startswith('final_src_'))
    return dict(old_cid=old['cid'], artifact_id=artifact_id, title=old['title'], has_dubbing=has_dubbing,
        **{k: metadata[k] for k in ('version_id', 'version_name', 'version_generated_name', 'version_summary') if k in metadata},
        generated_at=metadata.get('generated_at'), generated_at_source=metadata.get('generated_at_source', 'artifact_registered'),
        created_at=artifact_registered_at(artifact),
        registered_at=artifact_registered_at(artifact),
        artifact_name=artifact.name, path=str(path), sha256=artifact.sha256, size=path.stat().st_size,
        duration=pub._probe(path), replaced=True)


def _freeze_cover(key):
    if not isinstance(key, str):
        raise _error('请选择项目已保存的封面')
    path = pub._cover_path(key)
    sha = pub._file_hash(path)
    if not key.startswith(sha + '.'):
        raise _error('封面内容与登记不一致，请重新选择', 'PUBLISH_UPDATE_FILE_CHANGED')
    return dict(cover_id=key, path=str(path), sha256=sha, size=path.stat().st_size)


def _workflow_access(workflow_id, uid):
    """内部编排只能借用实际持有的本项目租约，HTTP 参数不能开启此通道。"""
    if not workflow_id:
        return False
    from dubforge import workflows
    if (workflows._leases.get(uid) != workflow_id
            or getattr(workflows._owner, 'key', None) != workflow_id):
        raise _error('自动更新任务未持有本项目运行权限', 'WORKFLOW_BUSY')
    return True


def plan(key, body, *, workflow_id='', expected=None):
    if not isinstance(body, dict) or set(body) - {'replacements', 'additions', 'values', 'chapters',
                                               'collection', 'part_titles', 'part_order', 'cover_id', 'delivery_mode'}:
        raise _error('更新参数无效，请重新检查更新方案')
    delivery_mode = body.get('delivery_mode', 'publish')
    if not isinstance(delivery_mode, str) or delivery_mode not in {'draft', 'upload', 'publish'}:
        raise _error('请选择有效的原稿交付方式')
    source = _source(key)
    owned = _workflow_access(workflow_id, source['project_uid'])
    # 固定任务标识封闭“任务已落库、流程关联尚未落库”的崩溃窗口。
    update_id = 'workflow-update-' + workflow_id if owned else None
    request_hash = pub._hash(dict(source=key, body=body, expected=expected)) if owned else ''
    if update_id:
        with Session(get_engine()) as session:
            previous = session.get(PublicationUpdate, update_id)
            if previous:
                if (previous.source_publication_id != key
                        or json.loads(previous.execution).get('workflow_request_hash') != request_hash):
                    raise _error('同一流程不能改用其他原稿或更新范围', 'PUBLISH_CONFLICT')
                return _public(previous)
    from dubforge import runtime_edit
    with pub._execution_gate():
        runtime_edit.assert_editable('project:' + source['project_uid'], allow_owned=owned)
        from dubforge.publishing import chapter_dispatch
        if key in chapter_dispatch._running:
            raise _error('原稿章节任务正在执行，请等待其结束后再更新', 'PUBLISH_UPDATE_BUSY')
        with Session(get_engine()) as session:
            assert_available(session, account=source['account_id'], bvid=source['bvid'])
            _assert_remote_free(session, source['account_id'], source['bvid'])
    remote = _remote(source)
    if expected is not None:
        if not owned:
            raise _error('只有已确认的自动流程可以使用冻结更新目标')
        if (source['revision'] != expected.get('source_revision')
                or remote['version'] != expected.get('remote_version')
                or [{k: v for k, v in part.items() if k != 'filename'}
                    for part in remote['videos']] != expected.get('remote_parts')):
            raise _error('制作期间原稿记录或B站分P已变化，请重新核对更新目标；尚未上传',
                         'PUBLISH_REMOTE_CHANGED')
    replacements = body.get('replacements', [])
    additions = body.get('additions', [])
    if (not isinstance(additions, list) or len(additions) + len(remote['videos']) > 100
            or any(not isinstance(v, dict) or set(v) != {'artifact_id', 'title'}
                   or type(v['artifact_id']) is not int or not isinstance(v['title'], str)
                   or not v['title'].strip() or len(v['title'].strip().encode('utf-16-le')) // 2 > 80
                   or any(ord(c) < 32 or ord(c) == 127 for c in v['title']) for v in additions)):
        raise _error('新增分P须选择有效成片和名称，更新后最多100个分P')
    additions = [{**v, 'title': v['title'].strip()} for v in additions]
    if (not isinstance(replacements, list) or len(replacements) > len(remote['videos'])
            or any(not isinstance(v, dict) or set(v) != {'old_cid', 'artifact_id'}
                or type(v['old_cid']) is not int or type(v['artifact_id']) is not int for v in replacements)
            or len({v['old_cid'] for v in replacements}) != len(replacements)):
        raise _error('每个原分P最多选择一个替换成片')
    if len({v['artifact_id'] for v in replacements + additions}) != len(replacements + additions):
        raise _error('不同分P请选择各自的成片，同一上传回执不能重复绑定两个分P')
    by_cid = {v['cid']: v for v in remote['videos']}
    if any(v['old_cid'] not in by_cid for v in replacements):
        raise _error('原分P已在B站变化，请重新读取', 'PUBLISH_REMOTE_CHANGED')
    if (replacements or additions or body.get('part_titles') or body.get('part_order')
            or body.get('values') or body.get('cover_id')) and remote.get('edit_supported') is not True:
        raise _error(remote.get('edit_limitation') or '该稿件暂不支持自动替换，请到官网处理')
    values = body.get('values', {})
    values = studio.validate_edit_values(values, before=remote['settings'], account=source['account_id'])
    values = {k: v for k, v in values.items() if remote.get('settings', {}).get(k) != v}
    if ('title' in values and not values['title']) or ('tags' in values and not values['tags']):
        raise _error('标题和标签不能为空')
    titles = body.get('part_titles', {})
    layout = studio.part_layout(remote['videos'], titles=titles)
    titles = {str(p['cid']): p['title'] for p in layout if str(p['cid']) in titles}
    order = body.get('part_order')
    identities = [v['cid'] for v in remote['videos']] + [-i - 1 for i in range(len(additions))]
    if order is not None and (not isinstance(order, list) or len(order) != len(identities)
            or any(type(i) is not int for i in order) or set(order) != set(identities)):
        raise _error('分P排序须包含全部原分P及明确新增项各一次，不会删除未选分P')
    cover = _freeze_cover(body['cover_id']) if body.get('cover_id') else None
    chapters = _bool_options(body.get('chapters', {}),
                            {'enabled', 'translate', 'instruction', 'permanent', 'overwrite_existing'})
    available = pub.candidates(source['project_uid'])
    source_items = available.get('chapters') or (source['snapshot'].get('chapter_options') or {}).get('items', [])
    chapter_options = {**pub._chapter_options(chapters, source_items),
                       'overwrite_existing': chapters.get('overwrite_existing', False)}
    if (owned and expected and 'chapter_items' in expected
            and chapter_options['items'] != expected['chapter_items']):
        raise _error('制作期间原视频章节资料已变化，请重新核对更新范围；尚未上传',
                     'PUBLISH_REMOTE_CHANGED')
    collection = _bool_options(body.get('collection', {}), {'enabled', 'season_id', 'section_id'})
    collection = dict(enabled=collection.get('enabled', False), season_id=collection.get('season_id'),
                      section_id=collection.get('section_id'))
    if collection['enabled']:
        if bool(collection['season_id']) != bool(collection['section_id']):
            raise _error('请选择完整的目标合集和小节')
        if collection['season_id']:
            metadata = pub.metadata(source['account_id'])
            season = next((s for s in metadata.get('seasons', []) if s['id'] == collection['season_id']), None)
            section = next((s for s in (season or {}).get('sections', []) if s['id'] == collection['section_id']), None)
            if not season or not section:
                raise _error('目标合集或小节不可用，请重新读取')
            collection.update(title=season['title'], section_title=section.get('title', ''))
    if not (replacements or additions or values or body.get('values') or titles or order or cover
            or chapter_options['enabled'] or collection['enabled']):
        raise _error('没有需要更新的内容，请选择新成片或修改资料')
    files = [_freeze_file(source, item, by_cid[item['old_cid']]) for item in replacements]
    for index, addition in enumerate(additions):
        files.append({**_freeze_file(source, addition, dict(cid=-index - 1, title=addition['title'])), 'added': True})
    for part in files:
        part['title'] = titles.get(str(part['old_cid']), part['title'])
    transfers = {}
    skipped_chapters = []
    if chapter_options['enabled'] and not files:
        known = _proven_parts(source)
        for video in remote['videos']:
            if video['cid'] not in known:
                skipped_chapters.append(video['title'])
                continue
            part, transfer = known[video['cid']]
            files.append({**copy.deepcopy(part), 'old_cid': transfer['cid'], 'replaced': False,
                          'title': titles.get(str(transfer['cid']), video['title'])})
            transfers[str(part['artifact_id'])] = {**copy.deepcopy(transfer), 'sha256': part.get('sha256', '')}
        if not files and not (replacements or additions or body.get('values') or titles or order or cover
                              or collection['enabled']):
            raise _error('现有分P没有可核验的制作时间轴，无法单独同步章节；请选择对应制作版本或先替换视频')
    from dubforge.publishing.chapters import freeze_parts
    files = freeze_parts(source['project_uid'], files, chapter_options)
    snapshot = dict(source_publication_id=key, source_revision=source['revision'], project_uid=source['project_uid'],
        account_id=source['account_id'], account_name=source['account_name'], bvid=source['bvid'], aid=remote['aid'],
        title=source['title'], parts=files, settings={**remote['settings'], **values},
        values=values, remote_version=remote['version'], remote_parts=copy.deepcopy(remote['videos']),
        chapters=chapter_options, chapter_options=chapter_options, collection=collection,
        part_titles=titles, part_order=order, cover=cover,
        **({'delivery_mode': delivery_mode} if 'delivery_mode' in body else {}))
    from dubforge.content_review_service import assert_snapshot
    assert_snapshot({**snapshot, 'parts': [p for p in files if p.get('replaced')]})
    execution = dict(stages={k: dict(status='pending', label=label) for k, label in _STAGES.items()},
        transfers=transfers, delivery={})
    if owned:
        execution.update(workflow_id=workflow_id, workflow_request_hash=request_hash)
    if not replacements and not additions and not cover:
        execution['stages']['upload']['status'] = 'skipped'
    preview_parts = []
    known = _proven_parts(source)
    selected = {p['old_cid']: p for p in files if p.get('replaced')}
    for old in remote['videos']:
        part = selected.get(old['cid'])
        previous = known.get(old['cid'], ({}, {}))[0]
        old_duration = old.get('duration') or previous.get('duration')
        preview_parts.append(dict(old_cid=old['cid'], title=old['title'], replaced=bool(part),
            old_duration=old_duration, new_duration=part.get('duration') if part else old_duration,
            artifact_name=part.get('artifact_name', '') if part else '',
            timeline_note=(part.get('chapter_timeline') or {}).get('label', '') if part else '保留当前视频'))
        preview_parts[-1]['new_title'] = titles.get(str(old['cid']), old['title'])
    preview_parts.extend(dict(old_cid=p['old_cid'], title=p['title'], added=True, replaced=True,
                             new_duration=p['duration'], artifact_name=p['artifact_name'])
                         for p in files if p.get('added'))
    preview = dict(source=_source_view(source), parts=preview_parts,
        has_dubbing=any((selected.get(p['cid']) or known.get(p['cid'], ({}, {}))[0]).get('has_dubbing')
                       for p in remote['videos']),
        before={k: remote['settings'].get(k) for k in values}, after=values,
        chapters=dict(**chapter_options, parts=[dict(title=p['title'], items=p.get('chapter_items', []),
            timeline=p.get('chapter_timeline'), timeline_note=(p.get('chapter_timeline') or {}).get('label', '')) for p in files]),
        collection=collection, cover=dict(cover_id=cover['cover_id']) if cover else None,
        part_order=order, warnings=['稿件修改可能需要B站重新审核，提交成功不等于审核通过。'] +
            ([f'以下分P没有可核验的制作时间轴，本次保留其B站章节：{"、".join(skipped_chapters)}']
             if skipped_chapters else []))
    row = PublicationUpdate(source_publication_id=key, project_uid=source['project_uid'],
        account_id=source['account_id'], bvid=source['bvid'], plan=pub._json(snapshot), preview=pub._json(preview),
        execution=pub._json(execution), fingerprint=pub._hash(snapshot))
    if update_id:
        row.id = update_id
    execution['update_id'] = row.id
    row.execution = pub._json(execution)
    with pub._lock, Session(get_engine()) as session:
        if update_id and (previous := session.get(PublicationUpdate, update_id)):
            if json.loads(previous.execution).get('workflow_request_hash') != request_hash:
                raise _error('同一流程的更新范围已变化', 'PUBLISH_CONFLICT')
            return _public(previous)
        latest = pub._get(session, Publication, key)
        lifecycle.assert_active(latest)
        if latest.revision != source['revision']:
            raise _error('原发布记录已变化，请重新检查', 'PUBLISH_CONFLICT')
        assert_available(session, account=source['account_id'], bvid=source['bvid'])
        session.add(row); session.commit(); session.refresh(row)
        return _public(row)


def _save(key, execution=None, **fields):
    with _lock, Session(get_engine()) as session:
        row = _get(key, session)
        if execution is not None:
            row.execution = pub._json(execution)
            # 阶段完成度采用加权平均；上传和章节整理中的细粒度进度不再被
            # “阶段尚未结束”压成 0%，总览与更新页可以持续显示真实进展。
            row.progress = _stage_progress(execution)['progress']
        for name, value in fields.items():
            setattr(row, name, value)
        row.updated_at = now()
        session.add(row); session.commit()


def _check(cancel):
    if cancel.is_set() or _stop.is_set():
        raise _error('更新已暂停，已上传文件和已确认结果保留', 'PUBLISH_UPDATE_PAUSED')


def _resume_feedback(execution, key, label):
    """受理继续时立即替换旧失败投影；已完成阶段和未知写入凭证保持原样。"""
    execution.update(phase=label, error='', error_code='', outcome='', next_check=0)
    stages = execution.get('stages', {})
    for name, node in stages.items():
        if isinstance(node, dict) and node.get('status') in {'failed', 'paused', 'running', 'waiting'}:
            node.update(status='pending', label=_STAGES.get(name, node.get('label', '')), error='', error_code='')
            if name != 'upload':
                node.pop('progress', None)
    current = next((node for name in _STAGES if isinstance(node := stages.get(name), dict)
                    and node.get('status') not in {'completed', 'skipped'}), None)
    if current is not None:
        current.update(status='queued', label=label)
    delivery = execution.get('delivery')
    if isinstance(delivery, dict):
        for node in [*delivery.get('parts', []), delivery.get('collection', {})]:
            if isinstance(node, dict) and node.get('status') in {'failed', 'paused', 'running'}:
                node.update(status='pending', error='', error_code='')
        if delivery.get('execution'):
            from dubforge.execution_progress import Recorder
            # Recorder 保留上次失败的历史；本轮只承诺已进入队列，不伪造模型已启动。
            delivery['execution'] = Recorder(previous=delivery['execution'], restart=True, owner='publication_update',
                run_id=key).phase('update.preparing', label, state='queued',
                detail='已受理，正在准备未完成部分；已完成的视频与设置将复用')


def action(key, body, *, workflow_id=''):
    if (not isinstance(body, dict) or set(body) - {'action', 'confirmed', 'fingerprint'}
            or body.get('action') not in {'start', 'retry', 'reconcile', 'pause', 'submit'}):
        raise _error('请选择开始、继续、核对或暂停更新')
    operation = body['action']
    current = view(key)
    owned = _workflow_access(workflow_id, current['project_uid'])
    if operation == 'pause':
        with _lock:
            current = view(key)
            with Session(get_engine()) as session:
                row = _get(key, session)
                if _has_unknown(json.loads(row.execution), session):
                    _save(key, status='unknown', phase='修改结果待核对，暂停不会解除未知保护')
                    return view(key)
            if current['status'] in {'completed', 'unknown'}:
                return current
            if key in _running:
                _running[key].set()
            else:
                _save(key, status='paused', phase='更新已暂停，已完成结果保留')
        return view(key)
    if body.get('confirmed') is not True:
        raise _error('请先核对更新方案并确认，本操作会修改原BV', 'PUBLISH_UPDATE_CONFIRM')
    with pub._lock, pub._execution_gate(), _lock, Session(get_engine()) as session:
        row = _get(key, session)
        if row.status == 'completed' or key in _running:
            return _public(row)
        if body.get('fingerprint') != row.fingerprint:
            raise _error('更新方案已变化，请重新检查', 'PUBLISH_CONFLICT')
        execution = json.loads(row.execution)
        if (row.status == 'unknown' or _has_unknown(execution, session)) and operation != 'reconcile':
            raise _error('上次修改结果尚未确认，请先核对，不能重复发送', 'PUBLISH_WRITE_UNKNOWN')
        if operation == 'start' and row.status != 'prepared':
            raise _error('此方案已经开始，请使用继续或核对')
        if not execution.get('confirmed_at') and (now() - row.created_at.replace(tzinfo=now().tzinfo)).total_seconds() > 600:
            raise _error('更新方案已超过10分钟，请重新检查', 'PUBLISH_CONFLICT')
        source = pub._get(session, Publication, row.source_publication_id)
        lifecycle.assert_active(source)
        snapshot = json.loads(row.plan)
        if pub._hash(snapshot) != row.fingerprint:
            raise _error('更新方案校验失败，请重新检查', 'PUBLISH_CONFLICT')
        delivery_mode = snapshot.get('delivery_mode', 'publish')
        if delivery_mode == 'draft':
            raise _error('此任务仅准备本地方案；请在发布设置选择上传或提交后重新检查项目',
                         'PUBLISH_UPDATE_DELIVERY_LIMIT')
        if operation == 'submit':
            if delivery_mode != 'upload' or row.status != 'uploaded':
                raise _error('请先完成仅上传，再明确确认提交原稿更新', 'PUBLISH_UPDATE_CONFIRM')
            # 投稿授权只由独立的提交动作产生，普通继续不提升已确认终点。
            execution['submission_confirmed_at'] = now().isoformat()
        if source.revision != snapshot['source_revision']:
            raise _error('原记录已变化，请重新核对', 'PUBLISH_CONFLICT')
        assert_available(session, account=row.account_id, bvid=row.bvid, excluding=key)
        _assert_remote_free(session, row.account_id, row.bvid, execution)
        from dubforge.publishing import chapter_dispatch
        if row.source_publication_id in chapter_dispatch._running:
            raise _error('原稿章节正在执行，请等待结束后继续更新', 'PUBLISH_UPDATE_BUSY')
        # 后处理不再读取本地成片；只在上传/提交尚未完成时冻结项目制作。
        if json.loads(row.execution).get('stages', {}).get('edit', {}).get('status') != 'completed':
            from dubforge import runtime_edit
            runtime_edit.assert_editable('project:' + row.project_uid, allow_owned=owned)
        row.status, row.error, row.error_code = 'queued', '', ''
        row.phase = '等待核对原稿结果' if operation == 'reconcile' else '准备更新原稿件'
        if operation != 'reconcile':
            _resume_feedback(execution, key, row.phase)
        execution.setdefault('confirmed_at', now().isoformat())
        row.execution, row.updated_at = pub._json(execution), now()
        cancellation = threading.Event()
        _running[key] = cancellation
        session.add(row); session.commit()
        try:
            _pool.submit(_execute, key, cancellation, operation == 'reconcile')
        except Exception:
            _running.pop(key, None)
            message = '后台任务队列暂不可用，已完成结果保留；请稍后继续更新'
            row.status, row.phase, row.error, row.error_code = 'paused', message, message, 'PUBLISH_UPDATE_QUEUE'
            execution.update(phase=message, error=message, error_code='PUBLISH_UPDATE_QUEUE')
            for stage in execution.get('stages', {}).values():
                if isinstance(stage, dict) and stage.get('status') == 'queued':
                    stage.update(status='paused', error=message, error_code='PUBLISH_UPDATE_QUEUE')
            delivery = execution.get('delivery', {})
            if delivery.get('execution'):
                from dubforge.execution_progress import Recorder
                delivery['execution'] = Recorder(previous=delivery['execution'], owner='publication_update',
                    run_id=key).finish('paused', phase='update.queue_failed', label='等待重新加入后台队列', detail=message)
            row.execution = pub._json(execution)
            session.add(row); session.commit()
            raise _error(message, 'PUBLISH_UPDATE_QUEUE') from None
    if operation == 'submit':
        from dubforge.workflows import track_confirmed_delivery
        track_confirmed_delivery(current['source_publication_id'], update_id=key)
    return view(key)


def _validate_files(snapshot):
    from dubforge.content_review_service import assert_snapshot
    from dubforge.publishing.review_draft import assert_snapshot_current
    effective = {**snapshot, 'parts': [p for p in snapshot['parts'] if p.get('replaced')]}
    assert_snapshot(effective)
    if effective['parts']:
        assert_snapshot_current(effective)
    for part in snapshot['parts']:
        if not part.get('replaced'):
            continue
        path = Path(part['path'])
        if not path.is_file() or path.stat().st_size != part['size'] or pub._file_hash(path) != part['sha256']:
            raise _error('确认后的新成片已改变或丢失，请重新检查更新方案', 'PUBLISH_UPDATE_FILE_CHANGED')
    cover = snapshot.get('cover')
    if cover:
        path = Path(cover['path'])
        if not path.is_file() or path.stat().st_size != cover['size'] or pub._file_hash(path) != cover['sha256']:
            raise _error('确认后的封面已变化，请重新检查', 'PUBLISH_UPDATE_FILE_CHANGED')


def _change(key):
    with Session(get_engine()) as session:
        return session.get(PublishChange, key) if key else None


def _verify_edit(key, source, snapshot, execution, change):
    """普通资料修改也需回读；接受回执不能证明最终资料已生效。"""
    execution['edit_unverified'] = True
    _save(key, execution)
    remote = _remote(source)
    expected = json.loads(change.preview)['after']
    if ([(p['cid'], p['title']) for p in remote['videos']]
            != [(p['cid'], p['title']) for p in expected['videos']]
            or any(remote['settings'].get(k) != v for k, v in expected.get('settings', {}).items())):
        raise _error('原稿更新已提交，视频或文案尚未回读一致，请稍后核对；未重复发送',
                     'PUBLISH_WRITE_UNKNOWN')
    execution['edit_unverified'] = False
    execution['final_parts'] = copy.deepcopy(expected['videos'])


def _execute(key, cancel, reconcile_only=False):
    execution = None
    _owner.key = key
    try:
        with Session(get_engine()) as session:
            row = _get(key, session)
            snapshot, execution = json.loads(row.plan), json.loads(row.execution)
        _check(cancel)
        if snapshot.get('delivery_mode') == 'draft':
            _save(key, execution, status='prepared', phase='本地更新方案已准备，尚未上传或提交')
            return
        source = dict(bvid=snapshot['bvid'], aid=snapshot['aid'], account_id=snapshot['account_id'])
        account = snapshot['account_id']
        if execution['stages']['edit']['status'] != 'completed':
            change = _change(execution.get('change_id'))
            if change and change.status in {'committing', 'unknown'}:
                result = studio.reconcile(account, change.id, {})
                if result['status'] != 'done':
                    execution['stages']['edit'].update(status='unknown', error='尚不能确认更新是否生效')
                    _save(key, execution, status='unknown', phase='原稿更新结果待核对',
                          error='尚不能确认更新是否生效，未重复发送', error_code='PUBLISH_WRITE_UNKNOWN')
                    return
                change = _change(change.id)
            if change and change.status == 'done':
                _verify_edit(key, source, snapshot, execution, change)
                execution['stages']['edit'].update(status='completed', error='')
                _save(key, execution)
            else:
                if reconcile_only:
                    _save(key, execution, status='paused', phase='未发现已发送的更新，请检查后继续')
                    return
                _validate_files(snapshot)
                remote = _remote(source)
                if remote['version'] != snapshot['remote_version']:
                    raise _error('B站稿件已在其他页面修改，请重新检查更新方案', 'PUBLISH_REMOTE_CHANGED')
                replacements = [p for p in snapshot['parts'] if p.get('replaced')]
                transfers = execution.setdefault('transfers', {})
                total = sum(p['size'] for p in replacements)
                loaded = sum(p['size'] for p in replacements if str(p['artifact_id']) in transfers)
                credential = pub._credential(account)
                for index, part in enumerate(replacements, 1):
                    _check(cancel)
                    part_key = str(part['artifact_id'])
                    if part_key in transfers:
                        continue
                    execution['stages']['upload'].update(status='running', progress=100 * loaded / max(1, total))
                    execution.update(current_part=index, loaded_bytes=loaded, total_bytes=total)
                    _save(key, execution, status='uploading', phase=f'上传修订视频：{part["title"]}')
                    def on_event(event):
                        amount = event.get('loaded')
                        if event.get('event') != 'progress' or type(amount) not in (int, float) or not math.isfinite(amount):
                            return
                        execution['loaded_bytes'] = loaded + max(0, min(part['size'], amount))
                        execution['stages']['upload']['progress'] = 100 * execution['loaded_bytes'] / max(1, total)
                        _save(key, execution)
                    transfer = transport.call('upload', dict(account_id=account, path=part['path'], title=part['title'],
                        artifact_id=part['artifact_id']),
                        credential=credential, cancel=cancel, on_event=on_event, timeout=24 * 3600)
                    if (not isinstance(transfer, dict) or type(transfer.get('cid')) is not int or transfer['cid'] <= 0
                            or not isinstance(transfer.get('filename'), str) or not transfer['filename']):
                        raise _error('新视频上传回执不完整，请重试上传', 'PUBLISH_UPDATE_UPLOAD_INVALID')
                    transfers[part_key] = transfer
                    loaded += part['size']
                    execution['loaded_bytes'] = loaded
                    _save(key, execution)
                values = copy.deepcopy(snapshot['values'])
                if snapshot.get('cover'):
                    if not execution.get('cover_url'):
                        from dubforge.publishing.covers import upload_path
                        _check(cancel)
                        execution['stages']['upload'].update(status='running', label='上传本次封面')
                        _save(key, execution, status='uploading', phase='上传更新封面')
                        uploaded = transport.call('cover', dict(account_id=account,
                            path=str(upload_path(Path(snapshot['cover']['path'])))),
                            credential=credential, cancel=cancel)
                        if not isinstance(uploaded, dict) or not uploaded.get('url'):
                            raise _error('封面上传回执不完整，请重试；视频上传结果保留', 'PUBLISH_COVER_RESPONSE')
                        execution['cover_url'] = studio.validate_edit_values({'cover': uploaded.get('url')})['cover']
                        _save(key, execution)
                    values['cover'] = execution['cover_url']
                # 平台可能复用同一图片地址，上传成功不代表稿件资料有实际变化。
                values = {name: value for name, value in values.items()
                          if remote.get('settings', {}).get(name) != value}
                execution['stages']['upload']['status'] = 'completed' if replacements or snapshot.get('cover') else 'skipped'
                _check(cancel)
                if snapshot.get('delivery_mode') == 'upload' and not execution.get('submission_confirmed_at'):
                    execution['stages']['edit']['status'] = 'waiting_confirmation'
                    _save(key, execution, status='uploaded', phase='替换文件已上传，等待确认更新原稿',
                          error='', error_code='')
                    return
                _validate_files(snapshot)
                expired = (change and change.status == 'prepared'
                           and (now() - change.created_at.replace(tzinfo=now().tzinfo)).total_seconds() > 600)
                if change is None or change.status == 'failed' or expired:
                    if replacements:
                        mapped = [dict(old_cid=0 if p.get('added') else p['old_cid'], **{k: transfers[str(p['artifact_id'])][k]
                            for k in ('cid', 'filename')}, title=p['title']) for p in replacements]
                        extras = {}
                        if snapshot.get('part_titles'):
                            extras['part_titles'] = snapshot['part_titles']
                        if snapshot.get('part_order') is not None:
                            new_ids = {p['old_cid']: transfers[str(p['artifact_id'])]['cid']
                                       for p in replacements if p.get('added')}
                            extras['order'] = [new_ids.get(cid, cid) for cid in snapshot['part_order']]
                        prepared = studio.prepare_replacement(account, snapshot['bvid'], mapped,
                            values=values, expected_version=snapshot['remote_version'], **extras)
                    elif values or snapshot.get('part_titles') or snapshot.get('part_order') is not None:
                        layout = studio.part_layout(remote['videos'], titles=snapshot.get('part_titles'),
                                                   order=snapshot.get('part_order'))
                        before_layout = [{k: p[k] for k in ('cid', 'filename', 'title')} for p in remote['videos']]
                        if values or layout != before_layout:
                            prepared = studio.prepare(account, dict(bvid=snapshot['bvid'], kind='edit', values=values,
                                **({'part_titles': snapshot['part_titles']} if snapshot.get('part_titles') else {}),
                                **({'order': snapshot['part_order']} if snapshot.get('part_order') is not None else {})),
                                expected_version=snapshot['remote_version'])
                        else:
                            prepared = None
                    else:
                        prepared = None
                    if prepared:
                        execution['change_id'] = prepared['id']
                        _save(key, execution)
                        change = _change(prepared['id'])
                if change:
                    execution['stages']['edit']['status'] = 'running'
                    _save(key, execution, status='submitting', phase='提交更新到原BV，保留其他分P')
                    _check(cancel)
                    if change.status != 'prepared':
                        raise _error('原稿修改结果待核对，未重复发送', 'PUBLISH_WRITE_UNKNOWN')
                    result = studio.commit(account, change.id, {'confirmed': True})
                    if result.get('status') != 'done':
                        raise _error('更新结果待核对，未重复发送', 'PUBLISH_WRITE_UNKNOWN')
                    _verify_edit(key, source, snapshot, execution, _change(change.id))
                else:
                    execution['final_parts'] = remote['videos']
                execution['stages']['edit'].update(status='completed', error='')
                _save(key, execution)
            if reconcile_only:
                _save(key, execution, status='paused', phase='原稿更新结果已核对，可继续剩余步骤', error='', error_code='')
                return
        _check(cancel)
        from dubforge.publishing import update_delivery
        _save(key, execution, status='delivering', phase='核对新视频与后续交付')
        execution = update_delivery.run(snapshot, execution,
            save=lambda value: _save(key, value, phase=value.get('phase', '核对更新结果')),
            cancel=cancel, read_only=reconcile_only)
        _check(cancel)
        outcome = execution.get('outcome', 'failed')
        status = {'completed': 'completed', 'waiting': 'waiting', 'unknown': 'unknown'}.get(outcome, 'failed')
        if reconcile_only and status == 'waiting':
            status = 'paused'
            execution['phase'] = '结果核对已完成，可继续剩余步骤'
        # 原始上传快照与回执身份保留，只刷新已有“平台状态观察”，避免更新后仍显示旧退回状态。
        try:
            pub.refresh_status(snapshot['source_publication_id'], cancel=cancel)
        except Exception:
            execution['receipt_sync_error'] = '原记录的状态观察暂未刷新，可点击查询平台状态；本次更新断点保留'
        _save(key, execution, status=status, phase=execution.get('phase', '更新后处理尚未完成'),
            error=execution.get('error', ''), error_code=execution.get('error_code', ''))
    except Exception as exc:
        code = getattr(exc, 'code', 'PUBLISH_UPDATE_FAILED')
        with Session(get_engine()) as session:
            unknown = _has_unknown(execution or {}, session)
        paused = code == 'PUBLISH_UPDATE_PAUSED' or cancel.is_set() or _stop.is_set()
        status = 'unknown' if unknown else 'paused' if paused else 'failed'
        message = exc.message if isinstance(exc, DubForgeError) else '原稿更新未完成，请核对任务详情后继续'
        for stage in (execution or {}).get('stages', {}).values():
            if stage.get('status') == 'running':
                stage.update(status=status, error=message)
        _save(key, execution, status=status, phase='原稿更新结果待核对' if unknown else message,
              error=message, error_code=code)
    finally:
        _owner.key = ''
        with _lock:
            _running.pop(key, None)


def recover():
    """重启不自动发送；所有已确认进度保留，用户明确继续后恢复。"""
    with Session(get_engine()) as session:
        for row in session.exec(select(PublicationUpdate).where(PublicationUpdate.status.in_(OPEN))).all():
            execution = json.loads(row.execution)
            row.status = 'unknown' if _has_unknown(execution, session) else 'paused'
            row.phase = '服务已重启，请核对结果后继续更新'
            row.updated_at = now()
            session.add(row)
        session.commit()


def _scan():
    while not _stop.wait(10):
        try:
            with Session(get_engine()) as session:
                keys = [row.id for row in session.exec(select(PublicationUpdate).where(PublicationUpdate.status == 'waiting')).all()
                        if json.loads(row.execution).get('next_check', 0) <= time.time()]
        except Exception:
            # 数据库临时忙碌不能让已确认任务的监控线程永久退出；下一轮仍只读核对。
            continue
        for key in keys:
            if _stop.is_set():
                return
            current = view(key)
            try:
                action(key, dict(action='retry', confirmed=True, fingerprint=current['fingerprint']))
            except DubForgeError:
                continue


def startup():
    global _monitor
    recover()
    _stop.clear()
    if _monitor is None or not _monitor.is_alive():
        _monitor = threading.Thread(target=_scan, name='publication-update-monitor', daemon=True)
        _monitor.start()


def shutdown():
    _stop.set()
    with _lock:
        for cancel in _running.values():
            cancel.set()
    if _monitor is not None and _monitor is not threading.current_thread():
        _monitor.join(timeout=1)
