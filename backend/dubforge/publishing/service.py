"""发布中心的唯一领域入口：继承、冻结、校验和恢复不散落在 API/界面里。"""
import copy
import hashlib
import json
import math
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from sqlmodel import Session, select

from dubforge import config, video_info, platform_cover as platform_images
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.domain.models import Artifact
from dubforge.pipeline.video_source import resolve_video
from dubforge.errors import DubForgeError
from dubforge.publishing import credentials, transport
from dubforge.publishing.models import Publication, PublishAccount, PublishCollectionRequest, now
from dubforge.publishing import source_credit
from dubforge.publishing import lifecycle
from dubforge.utils.fs import atomic_write_bytes


class PublishError(DubForgeError):
    code = 'PUBLISH_INVALID'
    suggestion = '请检查发布资料；保存不会投稿，仅上传文件与上传并投稿需要明确区分。'


DEFAULTS = dict(title='', description='', tags=[], tid=21, human_type2=0, copyright=2,
                source='', declaration_id=None, visibility='public', watermark=False,
                close_comments=False, close_danmaku=False, selected_comments=False,
                dynamic='', publish_at=None, season_id=None, section_id=None,
                topic_id=None, topic_name='', mission_id=None)
ACTIVE = {'queued', 'uploading', 'submitting', 'joining', 'checking'}
# 已取得BVID且平台交付已经结束的记录才允许在发布页生成远端同步候选。
# 上传中、结果未知和仅有本地失败记录不得绕过重复投稿保护。
REMOTE_COPY_STATUSES = {'submitted', 'published', 'private', 'reviewed', 'rejected', 'collection_pending'}


def _chapter_options(value=None, fallback=None):
    """冻结发布记录中的 YouTube 章节候选；章节同步不修改制作产物。"""
    value = value if isinstance(value, dict) else {}
    enabled = value.get('enabled', False)
    translate = value.get('translate', True)
    permanent = value.get('permanent', True)
    instruction = value.get('instruction', '')
    if type(enabled) is not bool or type(translate) is not bool or type(permanent) is not bool:
        raise PublishError('章节同步开关无效')
    if not isinstance(instruction, str) or len(instruction) > 2000:
        raise PublishError('章节标题自定义要求须为2000字以内的文本', code='PUBLISH_CHAPTER_INVALID')
    rows = value.get('items', fallback if isinstance(fallback, list) else [])
    if not isinstance(rows, list) or len(rows) > 100:
        raise PublishError('章节最多保留100条，请删减后重试')
    result = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('title'), str):
            raise PublishError('章节资料格式无效')
        title = row['title'].strip()
        if not title or len(title) > 150:
            raise PublishError('章节标题不能为空且不得超过150字')
        try:
            start = float(row.get('start_time'))
            end = float(row.get('end_time')) if row.get('end_time') is not None else None
        except (TypeError, ValueError):
            raise PublishError('章节时间格式无效') from None
        if not math.isfinite(start) or start < 0 or end is not None and (not math.isfinite(end) or end <= start):
            raise PublishError('章节时间范围无效')
        result.append({'title': title, 'start_time': round(start, 3),
                       'end_time': None if end is None else round(end, 3)})
    result.sort(key=lambda row: (row['start_time'], row['title']))
    for left, right in zip(result, result[1:]):
        if left['start_time'] == right['start_time']:
            raise PublishError('章节起点不能重复')
    return {'enabled': enabled, 'translate': translate, 'permanent': permanent,
            'instruction': instruction.strip(), 'items': result}
# 创作管理的能力边界是平台适配器的一部分，不能用一个总开关代替。
# read_scope 的取值：value=当前详情可展示；guard_only=只用于并发校验/保留，
# 不能对用户宣称已读取；none=当前适配器没有读取契约。
# write 的取值：remote_edit=已有稿件可预览确认后修改；
# submission_only=只在新投稿时有意义；read_only=可读但当前不写；
# unsupported=当前适配器没有可靠的读写契约。
STUDIO_FIELD_CAPABILITIES = {
    'title': dict(read_scope='value', write='remote_edit', reason='支持已有稿件预览后修改'),
    'description': dict(read_scope='value', write='remote_edit', reason='支持已有稿件预览后修改'),
    'tags': dict(read_scope='value', write='remote_edit', reason='支持已有稿件预览后修改'),
    'dynamic': dict(read_scope='value', write='remote_edit', reason='支持已有稿件预览后修改'),
    'declaration_id': dict(read_scope='value', write='remote_edit', reason='可显式选择；清空声明需按平台规则确认'),
    'part_order': dict(read_scope='value', write='remote_edit', reason='保留全部分P，仅调整顺序'),
    'collection': dict(read_scope='value', write='remote_edit', reason='支持转入、移出已读取的合集小节'),
    'append_part': dict(read_scope='value', write='remote_edit', reason='只追加新分P，不替换已有分P'),
    'copyright': dict(read_scope='value', write='remote_edit', reason='按版权、来源与声明的完整组合校验，修改可能重新审核'),
    'source': dict(read_scope='value', write='remote_edit', reason='支持更新转载来源，转载类型须保留有效来源'),
    'tid': dict(read_scope='value', write='remote_edit', reason='核对账号可用分区后更新，平台可能重新审核'),
    'human_type2': dict(read_scope='value', write='remote_edit', reason='核对账号可用分区后更新并回读'),
    'part_titles': dict(read_scope='value', write='remote_edit', reason='按分P编号精确改名，保留视频文件'),
    'visibility': dict(read_scope='guard_only', write='submission_only', reason='已有稿件当前不暴露可见值，请到官网处理'),
    'watermark': dict(read_scope='guard_only', write='submission_only', reason='当前投稿参数，已有稿件不作为通用编辑项'),
    'publish_at': dict(read_scope='guard_only', write='submission_only', reason='定时发布时间只在投稿阶段有意义'),
    'topic': dict(read_scope='guard_only', write='read_only', reason='活动/话题具有账号与活动条件，当前不自动改'),
    'selected_comments': dict(read_scope='none', write='submission_only', reason='平台编辑接口不保证生效，精选评论请到官网处理'),
    'close_comments': dict(read_scope='none', write='submission_only', reason='平台编辑接口不保证生效，请到官网处理'),
    'close_danmaku': dict(read_scope='none', write='submission_only', reason='平台编辑接口不保证生效，请到官网处理'),
    'subtitles': dict(read_scope='value', write='read_only', reason='可读取已有字幕，当前没有安全的上传/编辑契约'),
    'cover': dict(read_scope='value', write='remote_edit', reason='使用项目已保存的封面，上传后回读核验'),
    'dolby': dict(read_scope='guard_only', write='read_only', reason='成品编码与平台资格决定，当前只做对账'),
    'lossless_music': dict(read_scope='guard_only', write='read_only', reason='成品编码与平台资格决定，当前只做对账'),
    'commercial_promotion': dict(read_scope='none', write='unsupported', reason='没有可靠的独立商业推广读写契约'),
    'chapters': dict(read_scope='value', write='remote_edit', reason='按分P读取章节规则，预览确认后提交并回读核对'),
}
_lock = threading.RLock()
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='publish')
_login_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bili-login')
_running: dict[str, threading.Event] = {}
_logins = {}
_suggesting: set[str] = set()
_suggestion_progress: dict[str, dict] = {}
_creating_collections: set[str] = set()


@contextmanager
def _execution_gate():
    """发布锁在外、任务启动锁在内；编辑和启动使用同一顺序。"""
    from dubforge.runner import runner
    from dubforge.pipeline import dubbing_session
    with runner._start_lock, dubbing_session.gate:
        yield


@contextmanager
def _publication_edit(session, key, *, allow_owned=False):
    """关联项目的短暂编辑，自动流程仅凭已有执行身份获得写入资格。"""
    from dubforge import runtime_edit
    row = _get(session, Publication, key)
    lifecycle.assert_active(row)
    with runtime_edit.editing('project:' + row.project_uid, allow_owned=allow_owned):
        yield


@contextmanager
def _publication_start(key, action):
    """先登记准备占用，再释放全局门禁进行大文件预检；失败撤销占用。"""
    from dubforge import runtime_edit
    from dubforge.workflows import check_idle
    from dubforge.publishing.studio import publication_available
    with _lock, _execution_gate():
        with Session(get_engine()) as session:
            publication_available(session, key, allow_archived_check=action == 'check')
        record = view(key)
        check_idle(record['project_uid'])
        if key in _running or record['status'] in ACTIVE:
            raise PublishError('此稿件已有进行中的操作')
        if action != 'check':
            runtime_edit.assert_editable('project:' + record['project_uid'], allow_owned=True)
        cancellation = threading.Event()
        _running[key] = cancellation
    try:
        yield cancellation
    except BaseException:
        with _lock, _execution_gate():
            if _running.get(key) is cancellation:
                _running.pop(key, None)
        raise


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _get(session, model, key):
    row = session.get(model, key)
    if not row:
        raise PublishError('发布记录或账号不存在', code='PUBLISH_NOT_FOUND')
    return row


def _project(uid):
    project = crud.get_project(uid)
    if not project:
        raise PublishError('来源项目不存在，请返回项目列表', code='PROJECT_NOT_FOUND')
    return project


def _revision(row, revision):
    if type(revision) is not int or row.revision != revision:
        raise PublishError('资料已经变化，请重新读取后操作', code='PUBLISH_CONFLICT')


def validate_values(raw):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise PublishError('包含不支持的发布设置')
    values = copy.deepcopy(raw)
    for key, value in values.items():
        if key in {'title', 'description', 'source', 'dynamic', 'topic_name'}:
            limits = {'title': 80, 'description': 2000, 'source': 200, 'dynamic': 233, 'topic_name': 100}
            if not isinstance(value, str) or len(value.encode('utf-16-le')) // 2 > limits[key]:
                raise PublishError(f'{dict(title="标题", description="简介", source="来源", dynamic="动态", topic_name="话题名称")[key]}超过长度限制')
            values[key] = value.strip()
        elif key == 'tags':
            if (not isinstance(value, list) or len(value) > 10 or any(not isinstance(t, str)
                    or not t.strip() or len(t) > 20 or ',' in t or '，' in t for t in value)):
                raise PublishError('标签最多10个，每个1–20字，请分别添加')
            values[key] = list(dict.fromkeys(t.strip() for t in value))
        elif key in {'watermark', 'close_comments', 'close_danmaku', 'selected_comments'}:
            if type(value) is not bool:
                raise PublishError('开关必须明确选择开启或关闭')
        elif key == 'visibility':
            if not isinstance(value, str) or value not in {'public', 'self'}:
                raise PublishError('请选择公开或仅自己可见')
        elif key == 'copyright':
            if type(value) is not int or value not in {1, 2, 3}:
                raise PublishError('请选择原创、转载或其他创作声明，并确认相应权利')
        elif key == 'declaration_id':
            if value is not None and (type(value) is not int or value < -1):
                raise PublishError('创作声明编号无效')
        elif key in {'tid', 'human_type2'} and (type(value) is not int or value < 0):
            raise PublishError('请选择有效的分区或内容类型')
        elif value is not None and (type(value) is not int or value < 0 or key == 'publish_at' and value == 0):
            raise PublishError('分区、合集、声明和时间必须是有效数值')
    return values


def _serialize(row, *, session=None, lifecycle_context=None):
    if lifecycle_context is None:
        if session is None:
            with Session(get_engine()) as local:
                lifecycle_context = lifecycle.context(local, [row])
        else:
            lifecycle_context = lifecycle.context(session, [row])
    from dubforge.publishing import chapter_sync
    return dict(id=row.id, project_uid=row.project_uid, revision=row.revision, status=row.status,
                draft=json.loads(row.draft), snapshot=json.loads(row.snapshot),
                receipt=json.loads(row.receipt), progress=row.progress, progress_detail=_progress_detail(row), error=row.error,
                chapter_sync=chapter_sync.state_view(row), execution=_execution_view(row),
                **lifecycle.describe(row, lifecycle_context.get(row.id, '')),
                created_at=row.created_at.isoformat(), updated_at=row.updated_at.isoformat())


def _progress_detail(row):
    """传输细节只输出白名单字段，不把上传地址、文件路径或凭据带到列表。"""
    snapshot, transfers = json.loads(row.snapshot), json.loads(row.transfers)
    parts = snapshot.get('parts', [])
    total = sum(p.get('size', 0) for p in parts)
    completed = sum(str(p.get('artifact_id')) in transfers for p in parts)
    detail = transfers.get('_progress', {})
    phase = detail.get('phase', row.status)
    if phase not in {'queued', 'validating', 'preparing', 'uploading', 'merging', 'cover',
                     'uploaded', 'submitting', 'joining', 'checking', 'complete'}:
        phase = row.status
    loaded = detail.get('loaded', total * max(0, min(100, row.progress)) / 100)
    loaded = loaded if isinstance(loaded, (int, float)) and math.isfinite(loaded) else 0
    current = detail.get('current_part', 0)
    return dict(phase=phase, loaded=int(max(0, min(total, loaded))), total=total,
                completed_parts=completed, total_parts=len(parts),
                current_part=current if type(current) is int and 0 <= current <= len(parts) else 0,
                current_title=str(detail.get('current_title', ''))[:80])


def _update_status_view(row):
    """Return the small, read-only projection needed by project progress views.

    The full update record contains frozen replacement plans and chapter previews;
    those can be large and are only needed on the publication page.  Overview and
    project-list polling only need the current phase, progress and stage states.
    """
    # The update worker owns the stage weighting and legacy compatibility.  Use
    # its bounded projection here rather than maintaining a second progress
    # algorithm in the navigation/status endpoint.
    from dubforge.publishing import updates
    return updates.progress_summary(row)


def _publication_status(row, latest_update=None):
    result = dict(id=row.id, status=row.status, progress=row.progress,
                  lifecycle=lifecycle.state(row), progress_detail=_progress_detail(row), error=row.error,
                  execution=_execution_view(row), chapter_sync=json.loads(row.chapter_sync))
    if latest_update is not None:
        result['latest_update'] = _update_status_view(latest_update)
    return result


def _execution_view(row):
    """统一过程投影；稿件状态、上传字节和章节交付仍保留各自真值。"""
    from dubforge.execution_progress import normalize
    if row.id in _suggesting and row.id in _suggestion_progress:
        return normalize(_suggestion_progress[row.id], owner='publication', run_id=row.id)
    source = (json.loads(row.transfers).get('_progress') or {}).get('execution') or {}
    state = {'queued': 'queued', 'paused': 'paused', 'failed': 'failed', 'unknown': 'unknown',
             'uploaded': 'completed', 'rejected': 'attention', 'collection_pending': 'attention'}.get(row.status)
    if row.status in {'published', 'private', 'reviewed'}:
        state = 'completed'
    elif row.status == 'submitted':
        state = 'waiting'
    return normalize(source, state=state, owner='publication', run_id=row.id)


def project_statuses(uids):
    """项目列表批量读取本地摘要；发布仍独立于制作步骤和配置指纹。"""
    result = {uid: dict(id=None, status='pending', progress=0) for uid in uids}
    if not result:
        return result
    with Session(get_engine()) as session:
        rows = session.exec(select(Publication).where(Publication.project_uid.in_(result),
                            Publication.deleted_at.is_(None))
                            .order_by(Publication.updated_at.desc(), Publication.id.desc())).all()
        # 归档旧稿时会更新其时间戳，不能让这条旧记录盖过同项目仍在使用的活动稿。
        # 没有活动稿时才回退到最新未删除记录，以保留远端回执的只读状态。
        latest = {}
        active = {}
        for row in rows:
            latest.setdefault(row.project_uid, row)
            if row.archived_at is None:
                active.setdefault(row.project_uid, row)
        # The latest original-BV update is part of the delivery progress shown
        # on project cards and the overview.  Read all of them in one query so
        # refreshing a project list does not create an N+1 database pattern.
        publication_ids = [row.id for row in rows]
        latest_updates = {}
        if publication_ids:
            from dubforge.publishing.models import PublicationUpdate
            update_rows = session.exec(select(PublicationUpdate).where(
                PublicationUpdate.source_publication_id.in_(publication_ids)
            ).order_by(PublicationUpdate.updated_at.desc(), PublicationUpdate.id.desc())).all()
            for update in update_rows:
                latest_updates.setdefault(update.source_publication_id, update)
        for uid in result:
            row = active.get(uid) or latest.get(uid)
            if row is not None:
                result[uid] = _publication_status(row, latest_updates.get(row.id))
    return result


def catalog():
    with Session(get_engine()) as session:
        accounts = [dict(id=a.id, name=a.name, revision=a.revision)
                    for a in session.exec(select(PublishAccount)).all()]
        templates = []
    # 每次返回独立对象，避免 API 调用者修改进程内的能力声明。
    studio_capabilities = dict(
        edit_metadata=True, reorder_parts=True, append_parts=True,
        replace_parts=True, update_existing=True, review_draft_mode='new_submission',
        field_capabilities=copy.deepcopy(STUDIO_FIELD_CAPABILITIES),
    )
    return dict(accounts=accounts, templates=templates, worker_ready=transport.available(),
                defaults=DEFAULTS, engine='biliAPI 2.15.0（独立上传／投稿）',
                studio_capabilities=studio_capabilities)


def list_publications(uid=None, *, lifecycle_filter='active'):
    with Session(get_engine()) as session:
        query = lifecycle.apply_filter(select(Publication), lifecycle_filter).order_by(
            Publication.updated_at.desc(), Publication.id.desc())
        if uid:
            query = query.where(Publication.project_uid == uid)
        rows = session.exec(query.limit(200)).all()
        management = lifecycle.context(session, rows)
        return [_serialize(row, lifecycle_context=management) for row in rows]


def manage_record(key, action, body):
    """仅改变本地收纳状态；保留原稿、回执和引用，不发送平台请求。"""
    if action not in {'archive', 'delete', 'restore'} or (not isinstance(body, dict)
            or set(body) != {'revision', 'confirmed'} or body.get('confirmed') is not True):
        raise PublishError('请核对本地记录范围并确认操作', code='PUBLISH_RECORD_CONFIRM')
    with _lock, _execution_gate(), Session(get_engine()) as session:
        row = _get(session, Publication, key)
        _revision(row, body.get('revision'))
        busy = lifecycle.context(session, [row]).get(key, '')
        if busy:
            raise PublishError(busy, code='PUBLISH_RECORD_BUSY')
        current = lifecycle.state(row)
        target = {'archive': 'archived', 'delete': 'deleted', 'restore': 'active'}[action]
        if current != target:
            capability = lifecycle.describe(row)['lifecycle_actions'][action]
            if not capability['allowed']:
                raise PublishError(capability['reason'], code='PUBLISH_RECORD_PROTECTED')
            row.archived_at = now() if action == 'archive' else None
            row.deleted_at = now() if action == 'delete' else None
            row.revision += 1
            row.updated_at = now()
            session.add(row)
            session.commit()
            session.refresh(row)
        return _view(session, row)


def project_status(uid):
    """导航只读本地最新稿件；不查询平台、不进入制作步骤的状态机。"""
    _project(uid)
    return project_statuses([uid])[uid]


def _copy_request(body):
    """稿件与项目候选共用有界输入白名单，不传发布账号或权限设置。"""
    from dubforge.publishing.copywriting import COPY_FIELDS
    current = body.get('current', {})
    instruction = body.get('instruction', '')
    if not isinstance(current, dict) or set(current) - COPY_FIELDS:
        raise PublishError('AI 仅能辅助标题、简介、标签和粉丝动态')
    # 原视频标题可能比B站上限长；输入允许有界超长，输出仍按投稿限制校验。
    if any((not isinstance(value, list) or len(value) > 40 or any(not isinstance(t, str) or len(t) > 100 for t in value))
           if field == 'tags' else (not isinstance(value, str) or len(value) > 10000)
           for field, value in current.items()):
        raise PublishError('现有文案格式无效或过长，请精简后重试')
    current = copy.deepcopy(current)
    if not isinstance(instruction, str) or len(instruction) > 2000:
        raise PublishError('自定义文案提示词请控制在2000字以内')
    return current, instruction.strip()


def _copy_options(body):
    """候选文案附加项仅作为本次生成参数，不保存也不触发远端写入。"""
    from dubforge.publishing.copywriting import normalize_options
    try:
        return normalize_options(body.get('copy_options'))
    except ValueError as exc:
        raise PublishError(str(exc), code='PUBLISH_AI_INVALID') from None


def copy_preview(uid):
    from dubforge.publishing.project_copy import preview
    return preview(uid)


def suggest_project(uid, body):
    from dubforge.publishing.project_copy import suggest
    return suggest(uid, body)


def suggest(key, body):
    """生成候选文案但不保存；长网络请求不得持有发布锁或数据库事务。"""
    from dubforge.publishing.copywriting import generate
    current, instruction = _copy_request(body)
    options = _copy_options(body)
    with _lock, Session(get_engine()) as session:
        row = _get(session, Publication, key)
        _revision(row, body.get('revision'))
        # 已完成稿件也允许生成“候选”文案，供发布页的远端同步预览使用。
        # 生成本身不改本地快照或B站；真正写入仍由 studio 的预览/确认接口负责。
        try:
            receipt_bvid = json.loads(row.receipt or '{}').get('bvid')
        except (TypeError, ValueError):
            receipt_bvid = None
        if row.status not in {'draft', 'uploaded'} and not (receipt_bvid and row.status in REMOTE_COPY_STATUSES):
            raise PublishError('此稿件当前不可生成候选；请先取得B站稿件回执或复制为新草稿')
        if key in _suggesting:
            raise PublishError('本稿件正在生成文案，请稍候', code='PUBLISH_AI_BUSY')
        with _publication_edit(session, key, allow_owned=True):
            uid, revision = row.project_uid, row.revision
            _suggesting.add(key)
    try:
        from dubforge.execution_progress import Recorder
        # 候选生成不改草稿、上传回执或排序时间；运行中过程可只读轮询，
        # 完成历史随候选返回，由保存/同步动作另行决定是否应用文案。
        progress = Recorder(lambda value: _suggestion_progress.__setitem__(key, value),
                            owner='publication', run_id=key)
        result = generate(_project(uid), current, instruction, options, progress=progress)
        execution = progress.finish('completed', '候选文案已生成，尚未保存或同步到B站')
        with _lock, Session(get_engine()) as session:
            row = _get(session, Publication, key)
            _revision(row, revision)
            # 已有 BVID 的终态记录允许返回候选；仍要求远端回执存在，避免
            # 在状态变化或回执被清空后把候选误绑定到不可发布的记录。
            try:
                current_bvid = json.loads(row.receipt or '{}').get('bvid')
            except (TypeError, ValueError):
                current_bvid = None
            if row.status not in {'draft', 'uploaded'} and not (current_bvid and row.status in REMOTE_COPY_STATUSES):
                raise PublishError('稿件状态已变化，候选文案未应用', code='PUBLISH_CONFLICT')
        return dict(**result, revision=revision, execution=execution)
    finally:
        with _lock:
            _suggesting.discard(key)
            _suggestion_progress.pop(key, None)


def _eligible_file(artifact, project, root):
    path = (root / artifact.rel_path).resolve()
    if artifact.project_id != project.id or path.suffix.lower() not in {'.mp4', '.mov', '.mkv'}:
        return None
    if artifact.step_key == 'assemble' and artifact.name.startswith('final_') and path.is_relative_to(root):
        return path
    if artifact.step_key == 'download' and artifact.name == 'video.mp4':
        source = resolve_video(project, root)
        # 外部本地原片只能是该项目明确登记的那个文件，不能上传任意绝对路径。
        if source and source.resolve() == path:
            return path
    return None


def candidates(uid):
    project = _project(uid)
    with Session(get_engine()) as session:
        rows = session.exec(select(Artifact).where(Artifact.project_id == project.id,
                              Artifact.step_key.in_(['assemble', 'download'])).order_by(Artifact.created_at.desc())).all()
    seen, result = set(), []
    root = crud.project_dir_by_project(project).resolve()
    from dubforge.pipeline.assembly_versions import artifact_metadata, artifact_registered_at
    version_meta = artifact_metadata(uid)
    from dubforge.content_review_service import candidate_states
    review_meta = candidate_states(project, rows)
    from dubforge.publishing.review_draft import revision_metadata
    revision_meta = revision_metadata(project, rows)
    for row in rows:
        path = _eligible_file(row, project, root)
        if not path or row.rel_path in seen:
            continue
        seen.add(row.rel_path)
        registered_at = artifact_registered_at(row)
        result.append(dict(id=row.id, name=row.name, path=str(path), size=row.size, created_at=registered_at,
                           registered_at=registered_at, generated_at=None, generated_at_source='artifact_registered',
                           exists=path.is_file(), sha256=row.sha256,
                           is_source=row.step_key == 'download',
                           has_dubbing=row.step_key == 'assemble' and not row.name.startswith('final_src_'),
                           **review_meta.get(row.id, {}),
                           **revision_meta.get(row.id, {})))
        result[-1].update(version_meta.get(row.id, {}))
        if result[-1].get('is_review_revision') and not result[-1]['review_revision_current']:
            result[-1].update(review_ready=False, review_status='审查修订已失效',
                              review_reason=result[-1]['review_revision_reason'])
    from dubforge.publishing.project_copy import source_content
    base = source_content(project)
    thumbnail = base['thumbnail']
    return dict(project=dict(uid=uid, title=project.title), files=result,
                source_published_date=base['source_published_date'], suggestion=base['content'],
                chapters=base.get('chapters', []),
                thumbnail=thumbnail, platform_cover_available=platform_images.supported(thumbnail),
                thumbnail_preview=(f'/api/projects/{uid}/steps/download/video-info/cover'
                                   f'?v={hashlib.sha256(thumbnail.encode()).hexdigest()}'
                                   if platform_images.supported(thumbnail) else ''))


def create(uid, *, key=None):
    from dubforge import runtime_edit
    data = candidates(uid)
    from dubforge import configuration
    settings = configuration.resolve(uid)['values']
    row = Publication(project_uid=uid, draft=_json(dict(account_id=settings['publish.account_id'], template_id='', parts=[],
                      overrides={}, cover_id='', chapter_options=_chapter_options(dict(items=data.get('chapters', []),
                          enabled=settings['publish.chapters_enabled'], translate=settings['publish.chapters_translate'],
                          instruction=settings['publish.chapters_instruction'],
                          permanent=settings['publish.chapters_permanent'])),
                      content=data['suggestion'])))
    with _lock, runtime_edit.editing('project:' + uid, allow_owned=True), Session(get_engine()) as session:
        if key:
            existing = session.get(Publication, key)
            if existing:
                if existing.project_uid != uid:
                    raise PublishError('发布记录与来源项目不一致')
                return _serialize(existing, session=session)
            row.id = key
        session.add(row)
        session.commit()
        session.refresh(row)
        return _serialize(row, session=session)


def resolve_defaults(account_id, template_id='', overrides=None):
    """自动流程也使用相同的继承规则，但视频文案在制作完成后填充。"""
    with Session(get_engine()) as session:
        _get(session, PublishAccount, account_id)
        row = Publication(draft=_json(dict(account_id=account_id, template_id=template_id,
                            overrides=validate_values(overrides or {}))))
        settings, origins, _sources = _resolve(session, row)
        return settings, origins


def _resolve(session, row, *, include_overrides=True):
    from dubforge import configuration
    draft = json.loads(row.draft)
    settings, origins = copy.deepcopy(DEFAULTS), {key: '默认' for key in DEFAULTS}
    sources = {key: dict(kind='global') for key in DEFAULTS}
    content = draft.get('content', {})
    settings.update(content)
    origins.update({key: '视频资料' for key in content})
    sources.update({key: dict(kind='content') for key in content})
    data = configuration.resolve(row.project_uid or None)
    for key in DEFAULTS:
        path = 'publish.' + key
        if path in data['configured']:
            settings[key], origins[key] = data['values'][path], data['origins'][path]
            sources[key] = data['sources'][path]
    if include_overrides:
        settings.update(draft['overrides'])
        origins.update({key: '本稿件' for key in draft['overrides']})
        sources.update({key: dict(kind='publication') for key in draft['overrides']})
    return settings, origins, sources


def _view(session, row):
    """一次解析继承设置，完整详情也可直接随本地写操作返回。"""
    result = _serialize(row, session=session)
    from dubforge.publishing import updates
    changes = updates.list_updates(row.id)
    result['latest_update'] = changes[0] if changes else None
    replacement = updates.superseding_update(row.id)
    if replacement and result['chapter_sync'].get('status') not in {'completed', 'skipped'}:
        result['chapter_sync'] = {**result['chapter_sync'], 'status': 'superseded',
            'phase': '章节已由原稿更新任务接管，请在更新任务中查看或继续',
            'superseded_by_update': replacement['id']}
    if result['snapshot']:
        from dubforge.publishing.chapters import freeze_parts
        snapshot = result['snapshot']
        # 补齐历史章节和上传 cid 只影响展示，不能改变用户确认过的投稿快照摘要。
        result['chapter_parts'] = freeze_parts(row.project_uid, snapshot.get('parts', []),
                                               snapshot.get('chapter_options') or {}, strict=False)
        transfers = json.loads(row.transfers)
        for part in result['chapter_parts']:
            cid = (transfers.get(str(part.get('artifact_id'))) or {}).get('cid')
            if type(cid) is int or isinstance(cid, str) and cid.isdigit():
                part['chapter_cid'] = str(cid)
        result['settings'], result['origins'] = result['snapshot']['settings'], result['snapshot']['origins']
        result['sources'] = result['snapshot'].get('sources', {})
    else:
        if crud.get_project(row.project_uid) is None:
            # 项目删除后仍能查看、收纳和找回独立草稿，不能假装继承新的全局资料。
            base = {**copy.deepcopy(DEFAULTS), **copy.deepcopy(result['draft'].get('content', {}))}
            origins = {key: '本地保留资料' for key in base}
            sources = {key: dict(kind='content') for key in base}
        else:
            base, origins, sources = _resolve(session, row, include_overrides=False)
        result.update(base_settings=base, base_origins=origins, base_sources=sources)
        overrides = result['draft']['overrides']
        result.update(settings={**copy.deepcopy(base), **copy.deepcopy(overrides)},
                      origins={**origins, **{key: '本稿件' for key in overrides}},
                      sources={**copy.deepcopy(sources), **{key: dict(kind='publication') for key in overrides}})
    return result


def view(key):
    with Session(get_engine()) as session:
        return _view(session, _get(session, Publication, key))


def refresh_status(key, *, cancel=None):
    """只读核对已返回 BV 的远端审核状态，不上传、投稿或修改合集。"""
    record = view(key)
    receipt = record['receipt']
    bvid = receipt.get('bvid')
    if not bvid:
        raise PublishError('尚无BV号，不能核对平台审核状态')
    if cancel is not None and cancel.is_set():
        raise PublishError('操作已暂停')
    snapshot = record['snapshot']
    credential = _credential(snapshot['account_id'])
    from dubforge.execution_progress import Recorder
    progress = Recorder(lambda value: _update(key, execution=value), previous=record.get('execution'),
                        owner='publication', run_id=key)
    progress.phase('publish.review.check', '只读核对B站审核状态', state='waiting')
    try:
        status = transport.call('status', dict(account_id=snapshot['account_id'], bvid=bvid),
                                credential=credential, cancel=cancel)
    except Exception:
        progress.phase('publish.review.retry', '等待下次审核核对', state='retrying',
                       detail='本次只读核对未完成，保留原稿件和回执', next_check_at=time.time() + 15)
        raise
    state = status.get('state')
    # 先处理平台终态；审核退回不能被“合集未完成”覆盖，否则用户会得到错误的恢复指引。
    # 合集未完成不能被其它非终态查询覆盖；私密稿件也不能误标为公开发布。
    next_status = ('rejected' if state in {-2, -16}
                   else 'collection_pending' if snapshot.get('settings', {}).get('season_id')
                   and not receipt.get('collection_joined')
                   else 'reviewed' if state == 0 and snapshot.get('update_target')
                   else 'private' if state == 0 and snapshot.get('settings', {}).get('visibility') == 'self'
                   else 'published' if state == 0 else 'submitted')
    _update(key, status=next_status,
            receipt={**receipt, 'remote_state': state,
                     'state_desc': str(status.get('state_desc', ''))[:200],
                     'reject_reason': str(status.get('reject_reason', ''))[:2000],
                     'videos': [{k: part.get(k) for k in ('cid', 'title', 'state_desc', 'reject_reason', 'xcode_state')}
                                for part in status.get('videos', [])]})
    if next_status in {'published', 'private', 'reviewed'}:
        progress.finish('completed', 'B站审核已通过')
    elif next_status in {'rejected', 'collection_pending'}:
        progress.finish('attention', '平台审核或合集归属需要处理')
    else:
        progress.phase('publish.review.waiting', '等待B站审核，稍后只读核对', state='waiting',
                       next_check_at=time.time() + 15)
    return view(key)


def check_template_selection(session, selected, previous=''):
    if selected:
        raise PublishError('请在项目总览绑定系列模板；发布记录不再单独选择模板')


def save(key, body):
    from dubforge.workflows import check_idle
    from dubforge.publishing.studio import publication_available
    overrides = validate_values(body.get('overrides', {}))
    raw_chapter_options = body.get('chapter_options')
    parts = body.get('parts', [])
    if not isinstance(parts, list) or len(parts) > 100:
        raise PublishError('分P清单无效，首版最多选择100个分P')
    seen = set()
    for part in parts:
        if (not isinstance(part, dict) or set(part) != {'artifact_id', 'title'}
                or type(part['artifact_id']) is not int or part['artifact_id'] in seen
                or not isinstance(part['title'], str) or not part['title'].strip() or len(part['title']) > 80):
            raise PublishError('请选择不重复的成片，并填写每个分P的名称（1–80字）')
        seen.add(part['artifact_id'])
    with _lock, Session(get_engine()) as session, _publication_edit(session, key, allow_owned=True):
        row = _get(session, Publication, key)
        publication_available(session, key)
        check_idle(row.project_uid)
        _revision(row, body.get('revision'))
        if row.status == 'uploaded':
            draft = json.loads(row.draft)
            chapter_options = _chapter_options(raw_chapter_options if raw_chapter_options is not None
                                               else draft.get('chapter_options'))
            if any(body.get(k) != draft[k] for k in ('parts', 'account_id', 'template_id', 'cover_id')):
                raise PublishError('文件已上传，不能更改分P、账号、模板或封面；可修改投稿资料')
            snapshot = json.loads(row.snapshot)
            settings = validate_values({**snapshot.get('base_settings', snapshot['settings']), **overrides})
            _validate_local_settings(settings)
            draft['overrides'] = overrides
            draft['chapter_options'] = chapter_options
            row.revision += 1
            snapshot.update(settings=settings, confirmed_revision=row.revision,
                            chapter_options=chapter_options,
                            sources={**snapshot.get('base_sources', {}), **{key: dict(kind='publication') for key in overrides}},
                            origins={**snapshot.get('base_origins', snapshot['origins']),
                                     **{key: '本稿件' for key in overrides}})
            from dubforge.publishing.chapters import freeze_parts
            snapshot['parts'] = freeze_parts(row.project_uid, snapshot['parts'], chapter_options, refresh=True)
            row.draft, row.snapshot, row.updated_at, row.error = _json(draft), _json(snapshot), now(), ''
            session.add(row)
            session.commit()
            return view(key)
        if row.status != 'draft':
            raise PublishError('当前状态不能编辑，请等待完成或复制为新草稿')
        allowed = {a['id'] for a in candidates(row.project_uid)['files']}
        if seen - allowed:
            raise PublishError('选中的成片不属于本项目或已被移除')
        draft = json.loads(row.draft)
        chapter_options = _chapter_options(raw_chapter_options if raw_chapter_options is not None
                                           else draft.get('chapter_options'))
        check_template_selection(session, body.get('template_id'), draft.get('template_id'))
        for field in ('account_id', 'template_id', 'cover_id'):
            if not isinstance(body.get(field, ''), str):
                raise PublishError('发布资料格式无效')
            draft[field] = body.get(field, '')
        if draft['account_id']:
            _get(session, PublishAccount, draft['account_id'])
        if draft['cover_id']:
            _cover_path(draft['cover_id'])
        draft.update(parts=parts, overrides=overrides, chapter_options=chapter_options)
        from dubforge.publishing.review_draft import synchronize_source
        synchronize_source(draft, parts, row.project_uid)
        row.draft, row.revision, row.updated_at = _json(draft), row.revision + 1, now()
        _resolve(session, row)
        session.add(row)
        session.commit()
    return view(key)


def copy_draft(key, body=None):
    """只复制本地资料；同一请求重试复用记录，不扫描媒体或请求平台。"""
    from dubforge.publishing.studio import publication_available
    request_id = None
    if body:
        if not isinstance(body, dict) or set(body) != {'revision', 'request_id'}:
            raise PublishError('复制请求无效，请刷新后重试', code='PUBLISH_COPY_INVALID')
        if type(body['revision']) is not int:
            raise PublishError('复制请求版本无效，请刷新后重试', code='PUBLISH_COPY_INVALID')
        try:
            request_id = str(uuid.UUID(body['request_id']))
        except (ValueError, TypeError, AttributeError):
            raise PublishError('复制请求标识无效，请刷新后重试', code='PUBLISH_COPY_INVALID') from None
    elif body is not None and body != {}:
        raise PublishError('复制请求无效，请刷新后重试', code='PUBLISH_COPY_INVALID')
    with _lock, Session(get_engine()) as session, _publication_edit(session, key):
        publication_available(session, key)
        original = _get(session, Publication, key)
        if original.status == 'unknown' or original.status in ACTIVE:
            raise PublishError('请先核对当前投稿结果，暂不能复制重投')
        _project(original.project_uid)
        ident = uuid.uuid5(uuid.NAMESPACE_URL, f'dubforge:copy-draft:{key}:{request_id}').hex if request_id else None
        if ident and (existing := session.get(Publication, ident)):
            receipt = json.loads(existing.draft).get('copy_request', {})
            if (existing.project_uid != original.project_uid or receipt.get('source_id') != key
                    or receipt.get('source_revision') != body['revision']):
                raise PublishError('复制请求已经用于其他资料，请刷新后重试', code='PUBLISH_COPY_CONFLICT')
            # 即使原稿后来改了资料，响应丢失后的重试也只能打开已成功创建的那一份。
            return {**_view(session, existing), 'reused': True}
        if request_id:
            _revision(original, body['revision'])
        draft = json.loads(original.draft)
        if request_id:
            draft['copy_request'] = dict(source_id=key, source_revision=original.revision, request_id=request_id)
        row = Publication(project_uid=original.project_uid, draft=_json(draft))
        if ident:
            row.id = ident
        # 先准备详情，配置解析失败时不留下页面打不开的半成草稿。
        result = _view(session, row)
        session.add(row)
        session.commit()
        return {**result, 'reused': False}


def _cover_path(key):
    if not re.fullmatch(r'[a-f0-9]{64}\.(png|jpg|webp)', key):
        raise PublishError('封面标识无效')
    path = config.data_root() / 'publishing/covers' / key
    if not path.is_file():
        raise PublishError('封面文件不存在，请重新选择')
    return path


def store_cover(data):
    if not data or len(data) > 10 * 1024 * 1024:
        raise PublishError('封面大小必须在10MB以内')
    extension = platform_images.image_extension(data)
    if not extension:
        raise PublishError('请上传 PNG、JPEG 或 WebP 图片')
    key = hashlib.sha256(data).hexdigest() + '.' + extension
    path = config.data_root() / 'publishing/covers' / key
    if not path.is_file() or path.read_bytes() != data:
        atomic_write_bytes(path, data)
    return dict(cover_id=key)


def platform_cover(uid):
    return store_cover(platform_images.get(uid).read_bytes())


def _file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _probe(path):
    try:
        process = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
            'format=duration:stream=codec_type,width,height', '-of', 'json', str(path)],
            capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        data = json.loads(process.stdout)
        duration = float(data['format']['duration'])
        if process.returncode or not any(s.get('codec_type') == 'video' for s in data.get('streams', [])) or not 0 < duration <= 36000:
            raise ValueError()
        return duration
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        raise PublishError('视频无法读取或时长超过10小时，请检查成片和 ffprobe') from None


def _ai_notice_scope(snapshot):
    """提醒确认只关联本稿件的实际资料，不受进度、继承标签等运行信息影响。"""
    return _hash({key: snapshot.get(key) for key in ('account_id', 'settings', 'parts', 'cover_sha256')})


def _plan_result(snapshot, revision):
    return dict(snapshot=snapshot, fingerprint=_hash(snapshot), revision=revision,
                settings_display=settings_display(snapshot.get('account_id', ''), snapshot.get('settings', {}),
                                                  account_name=snapshot.get('account_name', '')),
                ai_declaration_acknowledged=snapshot.get('ai_declaration_acknowledgement') == _ai_notice_scope(snapshot))


def plan(key, *, progress=None):
    if progress:
        progress.phase('publish.preflight', '检查发布资料与文件')
    with Session(get_engine()) as session:
        row = _get(session, Publication, key)
        lifecycle.assert_active(row)
        if row.status != 'draft':
            snapshot = json.loads(row.snapshot)
            return _plan_result(snapshot, row.revision)
        draft = json.loads(row.draft)
        settings, origins, sources = _resolve(session, row)
        chapter_options = _chapter_options(draft.get('chapter_options'))
        base_settings, base_origins, base_sources = _resolve(session, row, include_overrides=False)
        settings = validate_values(settings)
        account = _get(session, PublishAccount, draft['account_id'])
        project = _project(row.project_uid)
        if not draft['parts']:
            raise PublishError('请选择至少一个成片；不会自动选择任何版本')
        _validate_local_settings(settings)
        cover = _cover_path(draft['cover_id'])
        files = []
        from dubforge.pipeline.assembly_versions import artifact_metadata, artifact_registered_at
        version_metadata = artifact_metadata(row.project_uid)
        root = crud.project_dir_by_project(project).resolve()
        for index, part in enumerate(draft['parts'], 1):
            if progress:
                progress.phase('publish.files.verify', '核验成片文件与内容审查',
                               completed=index - 1, total=len(draft['parts']), unit='分P',
                               metrics=dict(current_part=index, total_parts=len(draft['parts'])))
            artifact = _get(session, Artifact, part['artifact_id'])
            path = _eligible_file(artifact, project, root)
            if not path or not path.is_file():
                raise PublishError('选中成片已失效，请重新选择')
            from dubforge.content_review_service import assert_publishable
            assert_publishable(project, artifact)
            from dubforge.publishing.review_draft import assert_revision_current
            assert_revision_current(project, artifact, verify_hash=True)
            size = path.stat().st_size
            if size <= 0 or size > 16 * 1024 ** 3:
                raise PublishError('首版按16GB上限检查，超大文件权限尚未验证')
            if _file_hash(path) != artifact.sha256:
                raise PublishError('成片内容已经改变，请重新登记或重新生成，不能上传旧索引对应的新文件')
            files.append(dict(**part, path=str(path), sha256=artifact.sha256, size=size,
                              created_at=artifact_registered_at(artifact), registered_at=artifact_registered_at(artifact),
                              generated_at=None, generated_at_source='artifact_registered',
                              duration=_probe(path), has_dubbing=artifact.step_key == 'assemble' and not artifact.name.startswith('final_src_')))
            files[-1].update(version_metadata.get(artifact.id, {}))
        from dubforge.publishing.chapters import freeze_parts
        files = freeze_parts(row.project_uid, files, chapter_options)
        snapshot = dict(project_uid=row.project_uid, account_id=account.id, account_name=account.name,
                        settings=settings, origins=origins, sources=sources, parts=files, cover_path=str(cover),
                        cover_sha256=_file_hash(cover), confirmed_revision=row.revision,
                        base_settings=base_settings, base_origins=base_origins, base_sources=base_sources,
                        chapter_options=chapter_options)
        from dubforge.content_review_service import assert_snapshot
        assert_snapshot(snapshot)
        if progress:
            progress.phase('publish.preflight.ready', '发布资料核验完成',
                           completed=len(files), total=len(files), unit='分P')
        return _plan_result(snapshot, row.revision)


def _credential(account_id):
    with Session(get_engine()) as session:
        return credentials.unprotect(_get(session, PublishAccount, account_id).credential)


def metadata(account_id):
    from dubforge.publishing import display
    with Session(get_engine()) as session:
        account = _get(session, PublishAccount, account_id)
        revision, credential = account.revision, account.credential
    result = transport.call('metadata', dict(account_id=account_id), credential=credentials.unprotect(credential))
    # 复用原本就要读取的平台结果，不为界面名称再发一次网络请求。
    display.remember(account_id, revision, credential, result)
    return result


def settings_display(account_id, settings, *, metadata=None, account_name=''):
    """单稿件和全流程共用名称解析，显示信息必须留在稳定业务指纹之外。"""
    from dubforge.publishing import display
    with Session(get_engine()) as session:
        account = session.get(PublishAccount, account_id) if account_id else None
        if account is not None:
            return display.resolve(account.id, account.revision, account.credential, settings,
                                   account_name=account.name, metadata=metadata)
    return dict(account_id=account_id, account_name=account_name, category_title='',
                season_title='', section_title='', human_type2=settings.get('human_type2'),
                season_id=settings.get('season_id'), section_id=settings.get('section_id'),
                source='none', fetched_at='')


def check_account(account_id):
    """人工只读核验；仅说明检查时登录有效，不修改账号或项目。"""
    result = transport.call('account_check', dict(account_id=account_id),
                            credential=_credential(account_id), timeout=30)
    if (not isinstance(result, dict) or not isinstance(result.get('id'), str)
            or not isinstance(result.get('name'), str) or not 1 <= len(result['name']) <= 150):
        raise PublishError('B站账号校验响应不完整，暂时无法确认登录状态，请稍后重试',
                           code='PUBLISH_ACCOUNT_RESPONSE')
    if result['id'] != str(account_id):
        raise PublishError('登录账号与所选账号不一致，请重新扫码连接所选账号',
                           code='PUBLISH_ACCOUNT_MISMATCH')
    return dict(status='verified', id=result['id'], name=result['name'], checked_at=now().isoformat())


def create_collection(account_id, body):
    """确认后只创建空合集；不保存草稿、不上传视频、不自动重试不确定写入。"""
    allowed = {'request_id', 'confirmed', 'title', 'description', 'cover_id'}
    if not isinstance(body, dict) or set(body) - allowed or body.get('confirmed') is not True:
        raise PublishError('请确认账号与合集资料后再创建', code='PUBLISH_COLLECTION_CONFIRM')
    request_id, title = body.get('request_id'), body.get('title')
    description, cover_id = body.get('description', ''), body.get('cover_id')
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
        raise PublishError('创建请求标识无效，请重新打开新建合集窗口')
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 50:
        raise PublishError('请填写1至50字的合集名称')
    if not isinstance(description, str) or len(description) > 500:
        raise PublishError('合集简介请控制在500字以内')
    if not isinstance(cover_id, str):
        raise PublishError('请先选择合集封面')
    title, description = title.strip(), description.strip()
    fingerprint = _hash(dict(account_id=account_id, title=title, description=description, cover_id=cover_id))
    credential = _credential(account_id)

    def result(row):
        return dict(id=row.id, account_id=row.account_id, status=row.status,
                    season_id=row.season_id, title=row.title, error=row.error)

    with _lock, Session(get_engine()) as session:
        row = session.get(PublishCollectionRequest, request_id)
        if row and (row.account_id != account_id or row.fingerprint != fingerprint):
            raise PublishError('创建资料已变化，请重新确认', code='PUBLISH_CONFLICT')
        if not row:
            row = session.exec(select(PublishCollectionRequest).where(
                PublishCollectionRequest.fingerprint == fingerprint)).first()
        if row and row.status == 'created':
            return result(row)
        if row and row.status in {'submitting', 'unknown'}:
            raise PublishError('此前创建结果尚未确认，请刷新合集列表或到B站核对，不要重复创建',
                               code='PUBLISH_COLLECTION_UNKNOWN')
        # 不确定请求按账号阻断，改名称或换请求标识也不能绕过保护。
        pending = session.exec(select(PublishCollectionRequest).where(
            PublishCollectionRequest.account_id == account_id,
            PublishCollectionRequest.status.in_(['submitting', 'unknown']))).first()
        if pending or account_id in _creating_collections:
            raise PublishError('该账号有尚未确认的合集创建请求，请先刷新列表核对',
                               code='PUBLISH_COLLECTION_UNKNOWN')
        from dubforge.publishing.covers import upload_path
        cover = upload_path(_cover_path(cover_id))
        if row is None:
            row = PublishCollectionRequest(id=request_id, account_id=account_id,
                                           fingerprint=fingerprint, title=title)
        row.status, row.error = 'submitting', ''
        session.add(row); session.commit(); session.refresh(row)
        key = row.id
        _creating_collections.add(account_id)
    try:
        receipt = transport.call('create_collection', dict(account_id=account_id, title=title,
            description=description, cover_path=str(cover), confirmed=True), credential=credential)
        season_id = receipt.get('season_id') if isinstance(receipt, dict) else None
        if type(season_id) is not int or season_id <= 0:
            raise PublishError('B站创建回执不完整，请先核对合集列表', code='PUBLISH_COLLECTION_UNKNOWN')
        with _lock, Session(get_engine()) as session:
            row = _get(session, PublishCollectionRequest, key)
            row.status, row.season_id = 'created', season_id
            session.add(row); session.commit(); session.refresh(row)
            return result(row)
    except Exception as exc:
        # 仅工作进程明确确认“未发送创建”或“平台已拒绝”才允许同一请求重试。
        safe_to_retry = getattr(exc, 'code', '') in {'PUBLISH_COLLECTION_NOT_SENT', 'PUBLISH_COLLECTION_REJECTED'}
        with _lock, Session(get_engine()) as session:
            row = _get(session, PublishCollectionRequest, key)
            row.status = 'failed' if safe_to_retry else 'unknown'
            error_message = ('合集未创建成功，请检查资料、登录和网络后重试' if safe_to_retry
                         else '创建结果尚未确认，请刷新合集列表或到B站核对，不要重复创建')
            # 工作进程已脱敏并标明失败环节；不能再用通用文案覆盖真正原因。
            if isinstance(exc, transport.TransportError) and getattr(exc, 'code', '') in {
                    'PUBLISH_COLLECTION_NOT_SENT', 'PUBLISH_COLLECTION_REJECTED', 'PUBLISH_COLLECTION_UNKNOWN'}:
                error_message = exc.message
            row.error = error_message
            session.add(row); session.commit()
        raise PublishError(error_message, code=getattr(exc, 'code', 'PUBLISH_COLLECTION_UNKNOWN')
                           if safe_to_retry else 'PUBLISH_COLLECTION_UNKNOWN',
                           detail=transport._error_detail(getattr(exc, 'detail', None))) from None
    finally:
        with _lock:
            _creating_collections.discard(account_id)


def pending_collections(account_id):
    with _lock, Session(get_engine()) as session:
        _get(session, PublishAccount, account_id)
        rows = session.exec(select(PublishCollectionRequest).where(
            PublishCollectionRequest.account_id == account_id,
            PublishCollectionRequest.status.in_(['submitting', 'unknown']))).all()
        return [dict(id=row.id, title=row.title, busy=account_id in _creating_collections,
                     created_at=row.created_at.isoformat()) for row in rows]


def resolve_collection(account_id, key, body):
    if set(body) - {'confirmed', 'season_id'} or body.get('confirmed') is not True:
        raise PublishError('请先在B站核对创建结果，再确认处理', code='PUBLISH_COLLECTION_CONFIRM')
    season_id = body.get('season_id')
    if season_id is not None and (type(season_id) is not int or season_id <= 0):
        raise PublishError('请选择已创建的合集，或明确确认未创建')
    # 正在发送中的请求不能被标记为失败，从而绕过重复创建保护。
    with _lock, Session(get_engine()) as session:
        row = _get(session, PublishCollectionRequest, key)
        if row.account_id != account_id or row.status not in {'unknown', 'submitting'}:
            raise PublishError('待核对记录已变化，请重新读取', code='PUBLISH_CONFLICT')
        if account_id in _creating_collections:
            raise PublishError('创建请求仍在执行，请稍后核对', code='PUBLISH_COLLECTION_BUSY')
        expected_title = row.title
    if season_id is not None:
        seasons = metadata(account_id).get('seasons', [])
        if not any(s['id'] == season_id and s['title'] == expected_title for s in seasons):
            raise PublishError('请从此账号选择与待核对名称相同的合集')
    with _lock, Session(get_engine()) as session:
        row = _get(session, PublishCollectionRequest, key)
        if row.status not in {'unknown', 'submitting'} or account_id in _creating_collections:
            raise PublishError('创建记录已变化，请重新读取', code='PUBLISH_CONFLICT')
        row.status = 'created' if season_id else 'failed'
        row.season_id = season_id
        row.error = '' if season_id else '用户已在B站核对未创建，允许手动重试'
        session.add(row); session.commit()
    return dict(status='created' if season_id else 'failed', season_id=season_id)


def login_start():
    with _lock:
        for old_key, item in list(_logins.items()):
            if time.time() - item['created'] > 300:
                item['cancel'].set()
                del _logins[old_key]
        for active_key, item in _logins.items():
            if item['status'] in {'generating', 'waiting', 'scanned', 'verifying', 'saving'}:
                # 刷新页面后恢复原会话，不生成另一个二维码，也不要求盲等过期。
                return dict(id=active_key)
        key = uuid.uuid4().hex
        _logins[key] = dict(status='generating', url='', error='', code='', created=time.time(), cancel=threading.Event())
        _login_pool.submit(_login, key)
    return dict(id=key)


def _login(key):
    state = _logins[key]
    def event(data):
        with _lock:
            if state['cancel'].is_set():
                return
            if data.get('event') == 'qr':
                state['url'], state['status'] = data['url'], 'waiting'
            elif data.get('event') in {'scanned', 'verifying'}:
                state['status'] = data['event']
                if data['event'] == 'verifying':
                    state['url'] = ''
    try:
        result = transport.call('login', on_event=event, cancel=state['cancel'], timeout=240)
        with _lock:
            if state['cancel'].is_set():
                return
            state['status'], state['url'] = 'saving', ''
        encrypted = credentials.protect(result['credential'])
        with _lock, Session(get_engine()) as session:
            if state['cancel'].is_set():
                return
            account = session.get(PublishAccount, result['id']) or PublishAccount(id=result['id'], name=result['name'])
            account.name, account.credential = result['name'], encrypted
            session.add(account)
            session.commit()
            state['status'], state['url'] = 'connected', ''
    except Exception as exc:
        with _lock:
            if state['cancel'].is_set():
                return
            confirmed = state['status'] in {'verifying', 'saving'}
            # 工作者和凭据模块已经脱敏；未知异常不暴露可能包含 Cookie 的文本。
            if isinstance(exc, (transport.TransportError, credentials.CredentialError)):
                reason, code = exc.message, exc.code
            else:
                reason = ('保存账号失败，请检查本地数据库和磁盘后重新扫码' if state['status'] == 'saving'
                          else '登录处理失败，请重新获取二维码')
                code = 'PUBLISH_LOGIN_FAILED'
            state.update(status='expired' if code == 'PUBLISH_QR_EXPIRED' else 'failed',
                         error=('手机端已确认，但网页连接未完成：' if confirmed else '') + reason,
                         code=code, url='')


def login_view(key, cancel=False):
    with _lock:
        state = _logins.get(key)
        if not state:
            raise PublishError('登录会话已结束或服务已重启，请重新获取二维码', code='PUBLISH_LOGIN_NOT_FOUND')
        if cancel and state['status'] not in {'connected', 'failed', 'expired', 'cancelled'}:
            state['cancel'].set()
            state['status'], state['url'] = 'cancelled', ''
        return {field: state.get(field, '') for field in ('status', 'url', 'error', 'code')}


def _update(key, **fields):
    with _lock, Session(get_engine()) as session:
        row = _get(session, Publication, key)
        detail = fields.pop('progress_detail', None)
        execution = fields.pop('execution', None)
        previous_detail = json.loads(row.transfers).get('_progress') or {}
        if execution is not None:
            detail = {**(previous_detail if detail is None else detail), 'execution': execution}
        elif detail is not None and previous_detail.get('execution'):
            detail = {**detail, 'execution': previous_detail['execution']}
        if detail is not None:
            transfers = fields.get('transfers', json.loads(row.transfers))
            fields['transfers'] = {**transfers, '_progress': detail}
        elif 'transfers' in fields:
            previous = json.loads(row.transfers).get('_progress')
            if previous is not None:
                fields['transfers'] = {**fields['transfers'], '_progress': previous}
        for field, value in fields.items():
            setattr(row, field, _json(value) if field in {'snapshot', 'transfers', 'receipt'} else value)
        row.updated_at = now()
        session.add(row)
        session.commit()


def enqueue(key, body):
    action = body.get('action')
    if action not in {'upload', 'publish', 'submit', 'collection', 'check'}:
        raise PublishError('未知的发布动作')
    if 'acknowledge_ai_notice' in body and (type(body['acknowledge_ai_notice']) is not bool
                                           or action not in {'upload', 'publish', 'submit'}):
        raise PublishError('声明提醒确认参数无效')
    with _publication_start(key, action) as cancellation:
        record = view(key)
        pending_snapshot = None
        if record['revision'] != body.get('revision'):
            raise PublishError('资料已变化，请重新确认', code='PUBLISH_CONFLICT')
        if action in {'upload', 'publish', 'submit'} and record.get('snapshot'):
            from dubforge.content_review_service import assert_snapshot
            assert_snapshot(record['snapshot'])
        if action in {'upload', 'publish'}:
            if action == 'publish' and body.get('confirmed') is not True:
                raise PublishError('请先确认上传完成后正式投稿；仅上传文件不会投稿')
            if record['receipt'].get('bvid'):
                raise PublishError('已有投稿回执，不能重新上传或重复投稿')
            if record['status'] not in {'draft', 'paused', 'failed'}:
                raise PublishError('当前状态不允许重新上传')
            from dubforge.execution_progress import Recorder
            preparation = Recorder(lambda value: _update(key, execution=value),
                previous=record.get('execution'), owner='publication', run_id=key)
            preview = plan(key, progress=preparation)
            if preview['fingerprint'] != body.get('fingerprint'):
                raise PublishError('模板或资料已变化，请重新预检确认', code='PUBLISH_CONFLICT')
            snapshot = {**preview['snapshot'], 'publish_after_upload': action == 'publish'}
            if body.get('acknowledge_ai_notice') is True:
                snapshot['ai_declaration_acknowledgement'] = _ai_notice_scope(snapshot)
            pending_snapshot = snapshot
        elif action == 'submit':
            if record['status'] != 'uploaded' or body.get('confirmed') is not True:
                raise PublishError('请先完成上传，并明确确认本稿件的正式投稿')
            # 新版确认页附带精确快照；旧版独立投稿仍由修订号保护。
            if 'fingerprint' in body or body.get('acknowledge_ai_notice') is True:
                snapshot = record['snapshot']
                if body.get('fingerprint') != _hash(snapshot):
                    raise PublishError('稿件资料已变化，请重新确认', code='PUBLISH_CONFLICT')
                if body.get('acknowledge_ai_notice') is True:
                    pending_snapshot = {**snapshot, 'ai_declaration_acknowledgement': _ai_notice_scope(snapshot)}
        elif action == 'collection':
            if not record['receipt'].get('bvid') or not record['snapshot'].get('settings', {}).get('season_id'):
                raise PublishError('还没有可加入合集的已投稿稿件')
        elif action == 'check' and not record['receipt'].get('bvid'):
            raise PublishError('尚无BV号，请在官方创作中心核对后填写回执')
        if not transport.available():
            raise PublishError('发布组件尚未安装，不能开始上传或投稿')
        # 项目占用标记保护长预检，进程级锁只覆盖短暂的入队提交。
        # 暂停与提交使用同一边界：预检中收到停止就不启动上传或正式写请求。
        with _lock:
            if cancellation.is_set() or _running.get(key) is not cancellation:
                raise PublishError('发布准备已取消，未开始上传或投稿', code='PUBLISH_PREPARATION_CANCELLED')
            current = view(key)
            if current['revision'] != record['revision'] or current['status'] != record['status']:
                raise PublishError('发布记录已变化，请重新检查', code='PUBLISH_CONFLICT')
            queued = dict(status='queued', error='', progress_detail=dict(phase='queued'))
            if pending_snapshot is not None:
                queued['snapshot'] = pending_snapshot
            _update(key, **queued)
            try:
                _pool.submit(_execute, key, action, cancellation)
            except Exception as exc:
                # 没有执行线程就没有发送副作用，恢复可重试状态，不能留下永久 queued。
                _update(key, status=record['status'], snapshot=record['snapshot'],
                        error='发布任务未能加入执行队列，请重试',
                        progress_detail=record.get('progress_detail') or {})
                raise PublishError('发布任务未能加入执行队列，请重试', code='PUBLISH_QUEUE_UNAVAILABLE') from exc
    if action in {'submit', 'publish'}:
        from dubforge.workflows import track_confirmed_delivery
        track_confirmed_delivery(key)
    return view(key)


def pause(key):
    with _lock:
        row = view(key)
        if row['status'] in {'submitting', 'joining', 'checking'}:
            raise PublishError('请求已发往B站，请等待结果；中途退出不能撤回投稿')
        if key in _running:
            _running[key].set()
    return view(key)


def _validate_local_settings(settings):
    if bool(settings.get('topic_id')) != bool(settings.get('topic_name')) or (settings.get('mission_id') and not settings.get('topic_id')):
        raise PublishError('话题资料不完整，请重新选择话题或清除')
    if not settings['title']:
        raise PublishError('请填写正式标题')
    if not settings['human_type2'] or not settings['tags']:
        raise PublishError('请填写分区和至少一个标签')
    if settings['copyright'] == 2 and not settings['source']:
        raise PublishError('转载稿件必须填写来源')
    if settings['close_comments'] and settings['selected_comments']:
        raise PublishError('关闭评论与精选评论不能同时启用')
    scheduled = settings['publish_at']
    if scheduled and not time.time() + 300 <= scheduled <= time.time() + 15 * 86400:
        raise PublishError('定时发布时间须在5分钟到15天之间；不会自动改成立即发布')
    if settings['declaration_id'] is None:
        raise PublishError('请读取账号可用设置，并选择创作声明')
    if settings['copyright'] == 2 and settings['declaration_id'] != -1:
        raise PublishError('转载通道不提交创作声明。含AI配音请核对官方页面适用的稿件类型，可选择“其他创作声明”；这不代替原作者授权')
    if settings['season_id'] and not settings['section_id']:
        raise PublishError('请选择目标合集的小节')


def _category_ids(rows):
    result = set()
    for row in rows if isinstance(rows, list) else []:
        children = row.get('children')
        if isinstance(children, list) and children:
            result.update(_category_ids(children))
        elif type(row.get('id')) is int:
            result.add(row['id'])
    return result


def _validate_submission(snapshot, meta):
    settings = snapshot['settings']
    _validate_local_settings(settings)
    declaration = next((d for d in meta.get('declarations', []) if d['value'] == settings['declaration_id']), None)
    if not declaration:
        raise PublishError('请从账号当前可用的创作声明中选择；接口未返回声明时须先到官方网页核对，不能猜测编号')
    # 配音版本只能作为提醒依据，不据此强制指定声明；仍检查平台选项是否合法。
    if not settings['human_type2'] and settings['tid'] not in _category_ids(meta.get('categories', [])):
        raise PublishError('所选分区不在账号当前可用列表中，请重新读取账号设置')
    if settings['human_type2'] and settings['human_type2'] not in _category_ids(meta.get('human_categories', [])):
        raise PublishError('所选新版分区已不可用，请重新读取账号设置')
    if settings['season_id']:
        if (meta.get('collection_error') or meta.get('collections_complete') is False
                or not isinstance(meta.get('seasons'), list)):
            raise PublishError('暂时无法完整读取合集列表，尚不能核对目标；请刷新重试，不会自动取消加入合集',
                               code='PUBLISH_COLLECTION_UNVERIFIED')
        season = next((s for s in meta.get('seasons', []) if s['id'] == settings['season_id']), None)
        if not season or not any(s['id'] == settings['section_id'] for s in season['sections']):
            raise PublishError('目标合集或小节已不可用，请重新选择，或明确设置为不加入合集',
                               code='PUBLISH_COLLECTION_UNAVAILABLE')


def _execute(key, action, cancel):
    submit_started = False
    upload_preflight = False
    from dubforge.execution_progress import Recorder
    progress = None
    try:
        record = view(key)
        progress = Recorder(lambda value: _update(key, execution=value),
                            previous=record.get('execution'), owner='publication', run_id=key)
        progress.phase('publish.start', '开始已确认的发布操作')
        snapshot = record['snapshot']
        if action in {'upload', 'publish', 'submit'}:
            progress.phase('publish.review.validate', '核验当前内容审查与成片')
            from dubforge.content_review_service import assert_snapshot
            assert_snapshot(snapshot)
            from dubforge.publishing.review_draft import assert_snapshot_current
            assert_snapshot_current(snapshot)
        credential = _credential(snapshot['account_id'])
        base = dict(account_id=snapshot['account_id'])
        def remote(operation, payload=None, **kwargs):
            if cancel.is_set():
                raise PublishError('操作已暂停')
            return transport.call(operation, {**base, **(payload or {})}, credential=credential,
                                  cancel=cancel, **kwargs)
        if action in {'upload', 'publish'}:
            with Session(get_engine()) as session:
                transfers = json.loads(_get(session, Publication, key).transfers)
            # 账号能力不匹配时，在发送任何视频之前返回可编辑草稿。
            upload_preflight = not any(str(p['artifact_id']) in transfers for p in snapshot['parts'])
            total = sum(p['size'] for p in snapshot['parts'])
            # 仅完整分P可复用；暂停时的部分字节不假称跨进程断点续传。
            loaded = sum(p['size'] for p in snapshot['parts'] if str(p['artifact_id']) in transfers)
            detail = dict(phase='validating', loaded=loaded)
            _update(key, progress=100 * loaded / total, progress_detail=detail)
            progress.phase('publish.capabilities', '检查B站账号与投稿要求', state='waiting')
            _validate_submission(snapshot, remote('metadata'))
            upload_preflight = False
            _update(key, status='uploading')
            for index, part in enumerate(snapshot['parts'], 1):
                if cancel.is_set():
                    raise PublishError('操作已暂停')
                path = Path(part['path'])
                progress.phase('publish.files.verify', '核验上传文件', completed=index - 1,
                               total=len(snapshot['parts']), unit='分P', metrics=dict(current_part=index))
                if not path.is_file() or path.stat().st_size != part['size'] or _file_hash(path) != part['sha256']:
                    raise PublishError('确认后的成片已改变或丢失，已停止上传')
                part_key = str(part['artifact_id'])
                # 完整分P可恢复；未完成的分P重新上传，不冒称跨进程分片续传。
                if part_key not in transfers:
                    detail = dict(phase='preparing', loaded=loaded, current_part=index, current_title=part['title'])
                    _update(key, progress_detail=detail)
                    progress.phase('publish.upload.prepare', '准备上传线路', state='waiting',
                                   metrics=dict(current_part=index, total_parts=len(snapshot['parts'])))
                    def on_progress(event):
                        if event.get('event') == 'progress':
                            done = event.get('loaded')
                            if not isinstance(done, (int, float)) or not math.isfinite(done):
                                return
                            done = min(part['size'], max(0, done))
                            phase = event.get('phase', 'uploading')
                            if phase not in {'preparing', 'uploading', 'merging'}:
                                phase = 'uploading'
                            detail.update(phase=phase, loaded=loaded + done)
                            _update(key, progress=100 * (loaded + done) / total, progress_detail=detail)
                            progress.phase('publish.upload.' + phase,
                                {'preparing': '准备上传线路', 'uploading': '上传视频文件', 'merging': 'B站合并上传分片'}[phase],
                                completed=loaded + done, total=total, unit='字节',
                                metrics=dict(loaded_bytes=loaded + done, total_bytes=total,
                                             current_part=index, total_parts=len(snapshot['parts'])))
                    transfers[part_key] = remote('upload', part, on_event=on_progress, timeout=24 * 3600)
                    loaded += part['size']
                    detail.update(loaded=loaded)
                    _update(key, transfers=transfers, progress=100 * loaded / total, progress_detail=detail)
            detail = dict(phase='cover', loaded=total)
            _update(key, progress=100, progress_detail=detail)
            progress.phase('publish.cover.upload', '上传封面', state='waiting',
                           metrics=dict(reused='cover' in transfers))
            if 'cover' not in transfers:
                if _file_hash(Path(snapshot['cover_path'])) != snapshot['cover_sha256']:
                    raise PublishError('封面文件已变化')
                from dubforge.publishing.covers import upload_path
                transfers['cover'] = remote('cover', dict(path=str(upload_path(Path(snapshot['cover_path'])))))['url']
                _update(key, transfers=transfers)
            if action == 'publish':
                # 自动衔接由本次确认授权，不依赖浏览器轮询；不短暂解锁成可编辑的uploaded。
                _update(key, progress=100)
                action = 'submit'
            else:
                _update(key, status='uploaded', progress=100, progress_detail=dict(phase='uploaded', loaded=total))
                progress.finish('completed', '视频文件已上传，等待正式投稿确认')
        if action == 'submit':
            # 首次投稿前重新读取能力；校验失败没有副作用，允许恢复待投稿。
            _update(key, progress_detail=dict(phase='validating', loaded=sum(p['size'] for p in snapshot['parts'])))
            progress.phase('publish.submit.preflight', '正式投稿前核对平台要求', state='waiting')
            _validate_submission(snapshot, remote('metadata'))
            with Session(get_engine()) as session:
                transfers = json.loads(_get(session, Publication, key).transfers)
            videos = [{**transfers[str(p['artifact_id'])], 'title': p['title']} for p in snapshot['parts']]
            if len(videos) != len(snapshot['parts']) or not transfers.get('cover'):
                raise PublishError('上传记录不完整，不能投稿')
            # 与暂停共用锁：发送前的暂停不能因提交标记抢先写入而误成“结果不明”。
            with _lock:
                if cancel.is_set():
                    raise PublishError('文件已上传，已停止投稿；可检查资料后再确认投稿')
                # 长上传期间允许编辑规则；真正发送投稿前按当前规则再核对一次。
                from dubforge.content_review_service import assert_snapshot
                progress.phase('publish.submit.validate', '投稿前最后核验内容审查')
                assert_snapshot(snapshot)
                from dubforge.publishing.review_draft import assert_snapshot_current
                assert_snapshot_current(snapshot)
                _update(key, status='submitting', progress_detail=dict(phase='submitting', loaded=sum(p['size'] for p in snapshot['parts'])))
                submit_started = True
            progress.phase('publish.submit.receipt', '正式投稿请求已发送，等待B站回执', state='waiting')
            receipt = remote('submit', dict(confirmed=True, settings=snapshot['settings'],
                                            videos=videos, cover=transfers['cover']))
            _update(key, receipt=receipt, status='submitted', progress_detail=dict(phase='complete', loaded=sum(p['size'] for p in snapshot['parts'])))
            submit_started = False
            progress.phase('publish.review.waiting', '投稿回执已确认，等待平台审核', state='waiting')
            if snapshot['settings']['season_id']:
                from dubforge import workflows
                if workflows.post_processing_paused(key):
                    _update(key, status='collection_pending',
                            error='投稿回执已确认；已停止后续处理，尚未加入合集，无需重新投稿')
                    progress.finish('paused', '已保留投稿回执，未发送后续合集操作')
                    return
                action = 'collection'
        if action == 'collection':
            record = view(key)
            receipt = record['receipt']
            _update(key, status='joining', progress_detail=dict(phase='joining', loaded=sum(p['size'] for p in snapshot['parts'])))
            progress.phase('publish.collection.read', '读取已投稿稿件的分P回执', state='waiting')
            status = remote('status', dict(bvid=receipt['bvid']))
            if not status['videos']:
                raise PublishError('B站尚未返回分P信息，请稍后重试加入合集')
            progress.phase('publish.collection.join', '加入目标合集并核对回执', state='waiting')
            joined = remote('collection', dict(season_id=snapshot['settings']['season_id'],
                section_id=snapshot['settings']['section_id'], aid=receipt['aid'],
                cid=status['videos'][0]['cid'], title=snapshot['settings']['title']))
            if not isinstance(joined, dict) or joined.get('joined') is not True:
                raise PublishError('加入合集回执未确认，请稍后重试核对', code='PUBLISH_COLLECTION_JOIN_UNKNOWN')
            _update(key, receipt={**receipt, 'collection_joined': True}, status='submitted', progress_detail=dict(phase='complete', loaded=sum(p['size'] for p in snapshot['parts'])))
            progress.phase('publish.review.waiting', '合集归属已确认，等待平台审核', state='waiting')
        elif action == 'check':
            _update(key, status='checking', progress_detail=dict(phase='checking', loaded=sum(p['size'] for p in snapshot['parts'])))
            receipt = record['receipt']
            progress.phase('publish.review.check', '只读核对B站审核状态', state='waiting')
            status = remote('status', dict(bvid=receipt['bvid']))
            state = status.get('state')
            # 先处理平台终态；审核退回不能被“合集未完成”覆盖，否则会误导批次恢复。
            next_status = ('rejected' if state in {-2, -16}
                           else 'collection_pending' if snapshot['settings']['season_id'] and not receipt.get('collection_joined')
                           else 'reviewed' if state == 0 and snapshot.get('update_target')
                           else 'private' if state == 0 and snapshot['settings']['visibility'] == 'self'
                           else 'published' if state == 0 else 'submitted')
            _update(key, status=next_status,
                    receipt={**receipt, 'remote_state': state, 'state_desc': str(status.get('state_desc', ''))[:200],
                             'reject_reason': str(status.get('reject_reason', ''))[:2000],
                             'videos': [{k: p.get(k) for k in ('cid', 'title', 'state_desc', 'reject_reason', 'xcode_state')}
                                        for p in status.get('videos', [])]})
            if next_status in {'published', 'private', 'reviewed'}:
                progress.finish('completed', '平台审核已通过；章节按独立交付状态继续处理')
            elif next_status in {'rejected', 'collection_pending'}:
                progress.finish('attention', '平台审核或合集归属需要处理，请继续原稿件')
            else:
                progress.phase('publish.review.waiting', '等待B站审核，稍后只读核对', state='waiting')
    except Exception as exc:
        message = exc.message if isinstance(exc, DubForgeError) else '发布任务未能完成，请检查文件和账号状态'
        if action == 'collection' and not message.startswith('视频已投稿'):
            message = f'视频已投稿，加入合集未完成：{message}。无需重新上传或投稿。'
        status = ('unknown' if submit_started else 'collection_pending' if action == 'collection'
                  else 'uploaded' if action == 'submit' else 'submitted' if action == 'check'
                  else 'paused' if cancel.is_set() else 'failed')
        if upload_preflight and not cancel.is_set():
            _update(key, status='draft', snapshot={}, error=message)
        else:
            _update(key, status=status, error=message)
        if progress:
            progress.finish('unknown' if submit_started else 'paused' if cancel.is_set()
                            else 'attention' if action == 'collection' else 'failed', message)
    finally:
        with _lock:
            _running.pop(key, None)


def attach_receipt(key, body):
    """不明状态只读核对分P标识，不能靠相同标题判断投稿已成功。"""
    bvid = body.get('bvid', '')
    if not re.fullmatch(r'BV[0-9A-Za-z]{10}', bvid):
        raise PublishError('请填写完整BV号')
    with _lock:
        row = view(key)
        if row['status'] != 'unknown':
            raise PublishError('只有投稿结果待核对时可关联回执')
        snapshot = row['snapshot']
        result = transport.call('status', dict(account_id=snapshot['account_id'], bvid=bvid),
                                credential=_credential(snapshot['account_id']))
        with Session(get_engine()) as session:
            transfers = json.loads(_get(session, Publication, key).transfers)
        expected = [int(transfers[str(p['artifact_id'])]['cid']) for p in snapshot['parts']]
        if expected != [int(p['cid']) for p in result['videos']] or not result.get('aid'):
            raise PublishError('此BV的分P与本次上传不一致，未关联，也不会重新投稿')
        _update(key, receipt=dict(bvid=bvid, aid=result['aid']), status='submitted', error='')
    return view(key)


def recover():
    with _lock, Session(get_engine()) as session:
        for row in session.exec(select(Publication).where(Publication.status.in_(ACTIVE))).all():
            snapshot, receipt = json.loads(row.snapshot), json.loads(row.receipt)
            if row.status == 'submitting':
                row.status = 'unknown'
            elif receipt.get('bvid'):
                row.status = ('collection_pending' if snapshot.get('settings', {}).get('season_id') and not receipt.get('collection_joined') else 'submitted')
            else:
                row.status = 'paused'
            row.error = '上次操作中断，请核对状态后继续；不会自动投稿'
            session.add(row)
        session.commit()


def interrupt_all():
    """服务退出时停止本进程上传与扫码，投稿中的不明结果由状态机核对。"""
    with _lock:
        for cancellation in _running.values():
            cancellation.set()
        for state in _logins.values():
            state['cancel'].set()


def check_project_idle(uid):
    from dubforge import release_batches
    from dubforge.publishing import chapter_dispatch
    release_batches.check_idle(uid)
    from dubforge import project_batches
    project_batches.check_idle(uid)
    with _lock, Session(get_engine()) as session:
        from dubforge.publishing import updates
        updates.assert_available(session, uid=uid)
        row = session.exec(select(Publication).where(Publication.project_uid == uid,
                                                    Publication.status.in_(ACTIVE))).first()
        if row:
            raise PublishError('项目有进行中的发布任务，请先等待或暂停上传后再删除项目')
        if session.exec(select(Publication).where(Publication.project_uid == uid,
                Publication.id.in_(tuple(chapter_dispatch._running)))).first():
            raise PublishError('项目正在处理视频章节，请等待结束后再删除项目')


@contextmanager
def output_replacement(uid, *, deleting=False):
    """提交成片与发布入队互斥，不能在上传读取文件时替换其内容。"""
    with _lock, Session(get_engine()) as session:
        from dubforge.publishing import updates
        updates.assert_available(session, uid=uid, writing_only=not deleting)
        if deleting:
            from dubforge.workflows import check_idle
            check_idle(uid)
        active = any(row.status in ACTIVE or row.id in _running for row in session.exec(select(Publication).where(
            Publication.project_uid == uid)).all())
        if active:
            if deleting:
                raise PublishError('项目正在发布或上传尚未停止，暂不能回收成品', code='OUTPUT_FILE_BUSY')
            raise PublishError('项目正在发布，暂不能替换成品；请等待发布结束或暂停上传后重新生成',
                               code='ASSEMBLE_PUBLISH_BUSY')
        yield


@contextmanager
def project_removal(uid):
    """删除期间阻止新发布入队；统一先发布锁、再平台资料锁的顺序。"""
    from dubforge.workflows import check_idle
    with _lock:
        check_idle(uid)
        check_project_idle(uid)
        yield


def _collection_order_target(account_id, season_id, section_id):
    if (not isinstance(account_id, str) or not re.fullmatch(r'[1-9]\d*', account_id)
            or any(type(v) is not int or not 0 < v < 2**53 for v in (season_id, section_id))):
        raise PublishError('合集或账号资料无效，请重新选择', code='PUBLISH_COLLECTION_ORDER_INVALID')
    return dict(account_id=account_id, season_id=season_id, section_id=section_id)


def _collection_order_snapshot(value, season_id, section_id):
    """工作进程快照白名单；完整成员与确认指纹可以安全持久化，不含凭据。"""
    fail = lambda: PublishError('无法完整读取合集成员与顺序，请刷新后重试',
                                code='PUBLISH_COLLECTION_ORDER_RESPONSE')
    if not isinstance(value, dict) or not isinstance(value.get('section'), dict):
        raise fail()
    section, episodes, fingerprint = value['section'], value.get('episodes'), value.get('fingerprint')
    if (section.get('id') != section_id or section.get('season_id') != season_id
            or type(section.get('id')) is not int or type(section.get('season_id')) is not int
            or type(section.get('type')) is not int or section['type'] != 1
            or not isinstance(section.get('title'), str) or not section['title'].strip() or len(section['title']) > 200
            or not isinstance(episodes, list) or len(episodes) > 2000
            or not isinstance(fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', fingerprint)):
        raise fail()
    seen_ids, seen_aids, rows = set(), set(), []
    for i, item in enumerate(episodes):
        if (not isinstance(item, dict)
                or any(type(item.get(k)) is not int or not 0 < item[k] < 2**53 for k in ('id', 'aid', 'cid'))
                or item['id'] in seen_ids or item['aid'] in seen_aids
                or type(item.get('order')) is not int or item['order'] != i + 1
                or not isinstance(item.get('title'), str) or len(item['title']) > 500
                or not isinstance(item.get('bvid'), str)
                or (item['bvid'] and not re.fullmatch(r'BV[A-Za-z0-9]{10}', item['bvid']))):
            raise fail()
        seen_ids.add(item['id'])
        seen_aids.add(item['aid'])
        rows.append({k: item[k] for k in ('id', 'aid', 'cid', 'bvid', 'title', 'order')})
    safe = dict(section={k: section[k] for k in ('id', 'season_id', 'title', 'type')}, episodes=rows)
    # 与 Node JSON.stringify 的固定白名单顺序一致，防止保存不一致或被修改的确认快照。
    encoded = json.dumps(safe, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if hashlib.sha256(encoded).hexdigest() != fingerprint:
        raise fail()
    return dict(**safe, fingerprint=fingerprint)


def read_collection_order(account_id, season_id, section_id, *, cancel=None):
    """完整读取一个合集小节，作为批次确认或未知结果核对的只读基线。"""
    payload = _collection_order_target(account_id, season_id, section_id)
    result = transport.call('collection_order_read', payload, credential=_credential(account_id), timeout=60,
                            **({'cancel': cancel} if cancel is not None else {}))
    return _collection_order_snapshot(result, season_id, section_id)


def apply_collection_order(account_id, season_id, section_id, *, order, snapshot, confirmed=False, cancel=None):
    """单次应用已确认批次顺序，保留其他既有成员；发送后异常绝不自动重试。"""
    payload = _collection_order_target(account_id, season_id, section_id)
    if (confirmed is not True or not isinstance(order, list) or not order or len(order) > 2000
            or any(type(v) is not int or not 0 < v < 2**53 for v in order) or len(set(order)) != len(order)):
        raise PublishError('缺少有效的批次排序确认，请重新检查方案', code='PUBLISH_COLLECTION_ORDER_INVALID')
    baseline = _collection_order_snapshot(snapshot, season_id, section_id)
    credential = _credential(account_id)
    if cancel is not None and cancel.is_set():
        raise PublishError('合集排序已停止，请重新检查后确认继续；本次没有发送排序请求',
                           code='PUBLISH_COLLECTION_ORDER_NOT_SENT')
    try:
        result = transport.call('collection_order_apply', dict(**payload, order=copy.deepcopy(order),
            snapshot=baseline, confirmed=True), credential=credential, timeout=120, cancel=cancel)
    except transport.TransportError as exc:
        # 管道超时/中断不能证明工作进程尚未 POST；让协调器先核对，不能直接重复修改。
        if exc.code in {'PUBLISH_TRANSPORT', 'PUBLISH_REMOTE_ERROR'}:
            raise PublishError('合集排序结果尚未确认，请先核对平台顺序，不会自动重复排序',
                               code='PUBLISH_COLLECTION_ORDER_UNKNOWN') from exc
        raise
    if not isinstance(result, dict) or result.get('verified') is not True or type(result.get('changed')) is not bool:
        raise PublishError('合集排序没有返回完整核对结果，请先核对平台顺序', code='PUBLISH_COLLECTION_ORDER_UNKNOWN')
    try:
        verified = _collection_order_snapshot(result, season_id, section_id)
    except PublishError as exc:
        raise PublishError('合集排序回读结果不完整，请先核对平台顺序', code='PUBLISH_COLLECTION_ORDER_UNKNOWN') from exc
    return dict(**verified, verified=True, changed=result['changed'])


def verify_collection_order(account_id, season_id, section_id, *, order, snapshot):
    """只读核对未知排序，只有完整预期顺序匹配才解除保护；此接口不会修改远端。"""
    payload = _collection_order_target(account_id, season_id, section_id)
    if (not isinstance(order, list) or not order or len(order) > 2000
            or any(type(v) is not int or not 0 < v < 2**53 for v in order) or len(set(order)) != len(order)):
        raise PublishError('批次排序核对资料无效，请重新检查方案', code='PUBLISH_COLLECTION_ORDER_INVALID')
    baseline = _collection_order_snapshot(snapshot, season_id, section_id)
    result = transport.call('collection_order_verify', dict(**payload, order=copy.deepcopy(order), snapshot=baseline),
                            credential=_credential(account_id), timeout=60)
    if not isinstance(result, dict) or type(result.get('verified')) is not bool:
        raise PublishError('合集排序没有返回完整核对结果，请刷新后再核对', code='PUBLISH_COLLECTION_ORDER_RESPONSE')
    return dict(**_collection_order_snapshot(result, season_id, section_id), verified=result['verified'])
