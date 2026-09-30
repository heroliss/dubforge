"""发布中心的唯一领域入口：继承、冻结、校验和恢复不散落在 API/界面里。"""
import copy
import hashlib
import json
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
_lock = threading.RLock()
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='publish')
_login_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='bili-login')
_running: dict[str, threading.Event] = {}
_logins = {}
_suggesting: set[str] = set()
_creating_collections: set[str] = set()


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
            if value not in {'public', 'self'}:
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


def _serialize(row):
    return dict(id=row.id, project_uid=row.project_uid, revision=row.revision, status=row.status,
                draft=json.loads(row.draft), snapshot=json.loads(row.snapshot),
                receipt=json.loads(row.receipt), progress=row.progress, error=row.error,
                created_at=row.created_at.isoformat(), updated_at=row.updated_at.isoformat())


def catalog():
    with Session(get_engine()) as session:
        accounts = [dict(id=a.id, name=a.name, revision=a.revision)
                    for a in session.exec(select(PublishAccount)).all()]
        templates = []
    return dict(accounts=accounts, templates=templates, worker_ready=transport.available(),
                defaults=DEFAULTS, engine='biliAPI 2.15.0（独立上传／投稿）')


def list_publications(uid=None):
    with Session(get_engine()) as session:
        query = select(Publication).order_by(Publication.updated_at.desc())
        if uid:
            query = query.where(Publication.project_uid == uid)
        return [_serialize(row) for row in session.exec(query.limit(200)).all()]


def project_status(uid):
    """导航只读本地最新稿件；不查询平台、不进入制作步骤的状态机。"""
    _project(uid)
    with Session(get_engine()) as session:
        row = session.exec(select(Publication).where(Publication.project_uid == uid)
                           .order_by(Publication.updated_at.desc(), Publication.id.desc())).first()
        if not row:
            return dict(id=None, status='pending', progress=0)
        return dict(id=row.id, status=row.status, progress=row.progress)


def suggest(key, body):
    """生成候选文案但不保存；长网络请求不得持有发布锁或数据库事务。"""
    from dubforge.publishing.copywriting import COPY_FIELDS, generate
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
    if not isinstance(instruction, str) or len(instruction) > 1000:
        raise PublishError('文案要求请控制在1000字以内')
    with _lock, Session(get_engine()) as session:
        row = _get(session, Publication, key)
        _revision(row, body.get('revision'))
        if row.status not in {'draft', 'uploaded'}:
            raise PublishError('此稿件当前不可编辑；请等待上传完成或复制为新草稿')
        if key in _suggesting:
            raise PublishError('本稿件正在生成文案，请稍候', code='PUBLISH_AI_BUSY')
        uid, revision = row.project_uid, row.revision
        _suggesting.add(key)
    try:
        result = generate(_project(uid), current, instruction.strip())
        with _lock, Session(get_engine()) as session:
            row = _get(session, Publication, key)
            _revision(row, revision)
            if row.status not in {'draft', 'uploaded'}:
                raise PublishError('稿件状态已变化，候选文案未应用', code='PUBLISH_CONFLICT')
        return dict(**result, revision=revision)
    finally:
        with _lock:
            _suggesting.discard(key)


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
    from dubforge.pipeline.assembly_versions import artifact_metadata
    version_meta = artifact_metadata(uid)
    for row in rows:
        path = _eligible_file(row, project, root)
        if not path or row.rel_path in seen:
            continue
        seen.add(row.rel_path)
        result.append(dict(id=row.id, name=row.name, path=str(path), size=row.size, created_at=row.created_at.isoformat(),
                           exists=path.is_file(), sha256=row.sha256,
                           is_source=row.step_key == 'download',
                           has_dubbing=row.step_key == 'assemble' and not row.name.startswith('final_src_'),
                           **{k: v for k, v in version_meta.get(row.id, {}).items() if k != 'has_dubbing'}))
        if row.id in version_meta: result[-1]['has_dubbing'] = version_meta[row.id]['has_dubbing']
    info = video_info.view(uid).get('info') or {}
    source_url = source_credit.source_url(project)
    summary = info.get('summary_zh') if not info.get('summary_stale') else ''
    description = source_credit.append_attribution(summary or '', source_credit.attribution(project, info))
    thumbnail = (info.get('content') or {}).get('thumbnail', '')
    return dict(project=dict(uid=uid, title=project.title), files=result,
                source_published_date=source_credit.published_date(info),
                suggestion=dict(title=(info.get('title_zh') or info.get('title_original') or project.title),
                                description=description, source=source_url),
                thumbnail=thumbnail, platform_cover_available=platform_images.supported(thumbnail),
                thumbnail_preview=(f'/api/projects/{uid}/steps/download/video-info/cover'
                                   f'?v={hashlib.sha256(thumbnail.encode()).hexdigest()}'
                                   if platform_images.supported(thumbnail) else ''))


def create(uid, *, key=None):
    data = candidates(uid)
    from dubforge import configuration
    settings = configuration.resolve(uid)['values']
    row = Publication(project_uid=uid, draft=_json(dict(account_id=settings['publish.account_id'], template_id='', parts=[],
                      overrides={}, cover_id='',
                      content=data['suggestion'])))
    with _lock, Session(get_engine()) as session:
        if key:
            existing = session.get(Publication, key)
            if existing:
                if existing.project_uid != uid:
                    raise PublishError('发布记录与来源项目不一致')
                return _serialize(existing)
            row.id = key
        session.add(row)
        session.commit()
        session.refresh(row)
        return _serialize(row)


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


def view(key):
    with Session(get_engine()) as session:
        row = _get(session, Publication, key)
        result = _serialize(row)
        if result['snapshot']:
            result['settings'], result['origins'] = result['snapshot']['settings'], result['snapshot']['origins']
            result['sources'] = result['snapshot'].get('sources', {})
        else:
            result['settings'], result['origins'], result['sources'] = _resolve(session, row)
            result['base_settings'], result['base_origins'], result['base_sources'] = _resolve(session, row, include_overrides=False)
    return result


def check_template_selection(session, selected, previous=''):
    if selected:
        raise PublishError('请在项目总览绑定系列模板；发布记录不再单独选择模板')


def save(key, body):
    from dubforge.workflows import check_idle
    from dubforge.publishing.studio import publication_available
    overrides = validate_values(body.get('overrides', {}))
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
    with _lock, Session(get_engine()) as session:
        row = _get(session, Publication, key)
        publication_available(session, key)
        check_idle(row.project_uid)
        _revision(row, body.get('revision'))
        if row.status == 'uploaded':
            draft = json.loads(row.draft)
            if any(body.get(k) != draft[k] for k in ('parts', 'account_id', 'template_id', 'cover_id')):
                raise PublishError('文件已上传，不能更改分P、账号、模板或封面；可修改投稿资料')
            snapshot = json.loads(row.snapshot)
            settings = validate_values({**snapshot.get('base_settings', snapshot['settings']), **overrides})
            _validate_local_settings(settings)
            draft['overrides'] = overrides
            row.revision += 1
            snapshot.update(settings=settings, confirmed_revision=row.revision,
                            sources={**snapshot.get('base_sources', {}), **{key: dict(kind='publication') for key in overrides}},
                            origins={**snapshot.get('base_origins', snapshot['origins']),
                                     **{key: '本稿件' for key in overrides}})
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
        check_template_selection(session, body.get('template_id'), draft.get('template_id'))
        for field in ('account_id', 'template_id', 'cover_id'):
            if not isinstance(body.get(field, ''), str):
                raise PublishError('发布资料格式无效')
            draft[field] = body.get(field, '')
        if draft['account_id']:
            _get(session, PublishAccount, draft['account_id'])
        if draft['cover_id']:
            _cover_path(draft['cover_id'])
        draft.update(parts=parts, overrides=overrides)
        row.draft, row.revision, row.updated_at = _json(draft), row.revision + 1, now()
        _resolve(session, row)
        session.add(row)
        session.commit()
    return view(key)


def copy_draft(key):
    from dubforge.publishing.studio import publication_available
    with _lock, Session(get_engine()) as session:
        publication_available(session, key)
        original = _get(session, Publication, key)
        if original.status == 'unknown' or original.status in ACTIVE:
            raise PublishError('请先核对当前投稿结果，暂不能复制重投')
        _project(original.project_uid)
        draft = json.loads(original.draft)
        row = Publication(project_uid=original.project_uid, draft=_json(draft))
        session.add(row)
        session.commit()
        session.refresh(row)
        return _serialize(row)


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
                ai_declaration_acknowledged=snapshot.get('ai_declaration_acknowledgement') == _ai_notice_scope(snapshot))


def plan(key):
    with Session(get_engine()) as session:
        row = _get(session, Publication, key)
        if row.status != 'draft':
            snapshot = json.loads(row.snapshot)
            return _plan_result(snapshot, row.revision)
        draft = json.loads(row.draft)
        settings, origins, sources = _resolve(session, row)
        base_settings, base_origins, base_sources = _resolve(session, row, include_overrides=False)
        settings = validate_values(settings)
        account = _get(session, PublishAccount, draft['account_id'])
        project = _project(row.project_uid)
        if not draft['parts']:
            raise PublishError('请选择至少一个成片；不会自动选择任何版本')
        _validate_local_settings(settings)
        cover = _cover_path(draft['cover_id'])
        files = []
        from dubforge.pipeline.assembly_versions import artifact_metadata
        version_metadata = artifact_metadata(row.project_uid)
        root = crud.project_dir_by_project(project).resolve()
        for part in draft['parts']:
            artifact = _get(session, Artifact, part['artifact_id'])
            path = _eligible_file(artifact, project, root)
            if not path or not path.is_file():
                raise PublishError('选中成片已失效，请重新选择')
            size = path.stat().st_size
            if size <= 0 or size > 16 * 1024 ** 3:
                raise PublishError('首版按16GB上限检查，超大文件权限尚未验证')
            if _file_hash(path) != artifact.sha256:
                raise PublishError('成片内容已经改变，请重新登记或重新生成，不能上传旧索引对应的新文件')
            files.append(dict(**part, path=str(path), sha256=artifact.sha256, size=size,
                              duration=_probe(path), has_dubbing=version_metadata.get(artifact.id, {}).get('has_dubbing',
                                  artifact.step_key == 'assemble' and not artifact.name.startswith('final_src_'))))
        snapshot = dict(project_uid=row.project_uid, account_id=account.id, account_name=account.name,
                        settings=settings, origins=origins, sources=sources, parts=files, cover_path=str(cover),
                        cover_sha256=_file_hash(cover), confirmed_revision=row.revision,
                        base_settings=base_settings, base_origins=base_origins, base_sources=base_sources)
        return _plan_result(snapshot, row.revision)


def _credential(account_id):
    with Session(get_engine()) as session:
        return credentials.unprotect(_get(session, PublishAccount, account_id).credential)


def metadata(account_id):
    return transport.call('metadata', dict(account_id=account_id), credential=_credential(account_id))


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
        for field, value in fields.items():
            setattr(row, field, _json(value) if field in {'snapshot', 'transfers', 'receipt'} else value)
        row.updated_at = now()
        session.add(row)
        session.commit()


def enqueue(key, body):
    from dubforge.publishing.studio import publication_available
    action = body.get('action')
    if action not in {'upload', 'publish', 'submit', 'collection', 'check'}:
        raise PublishError('未知的发布动作')
    if 'acknowledge_ai_notice' in body and (type(body['acknowledge_ai_notice']) is not bool
                                           or action not in {'upload', 'publish', 'submit'}):
        raise PublishError('声明提醒确认参数无效')
    with _lock:
        with Session(get_engine()) as session:
            publication_available(session, key)
        record = view(key)
        from dubforge.workflows import check_idle
        check_idle(record['project_uid'])
        if record['revision'] != body.get('revision'):
            raise PublishError('资料已变化，请重新确认', code='PUBLISH_CONFLICT')
        if key in _running or record['status'] in ACTIVE:
            raise PublishError('此稿件已有进行中的操作')
        if action in {'upload', 'publish'}:
            if action == 'publish' and body.get('confirmed') is not True:
                raise PublishError('请先确认上传完成后正式投稿；仅上传文件不会投稿')
            if record['receipt'].get('bvid'):
                raise PublishError('已有投稿回执，不能重新上传或重复投稿')
            if record['status'] not in {'draft', 'paused', 'failed'}:
                raise PublishError('当前状态不允许重新上传')
            preview = plan(key)
            if preview['fingerprint'] != body.get('fingerprint'):
                raise PublishError('模板或资料已变化，请重新预检确认', code='PUBLISH_CONFLICT')
            snapshot = {**preview['snapshot'], 'publish_after_upload': action == 'publish'}
            if body.get('acknowledge_ai_notice') is True:
                snapshot['ai_declaration_acknowledgement'] = _ai_notice_scope(snapshot)
            _update(key, snapshot=snapshot)
        elif action == 'submit':
            if record['status'] != 'uploaded' or body.get('confirmed') is not True:
                raise PublishError('请先完成上传，并明确确认本稿件的正式投稿')
            # 新版确认页附带精确快照；旧版独立投稿仍由修订号保护。
            if 'fingerprint' in body or body.get('acknowledge_ai_notice') is True:
                snapshot = record['snapshot']
                if body.get('fingerprint') != _hash(snapshot):
                    raise PublishError('稿件资料已变化，请重新确认', code='PUBLISH_CONFLICT')
                if body.get('acknowledge_ai_notice') is True:
                    _update(key, snapshot={**snapshot, 'ai_declaration_acknowledgement': _ai_notice_scope(snapshot)})
        elif action == 'collection':
            if not record['receipt'].get('bvid') or not record['snapshot'].get('settings', {}).get('season_id'):
                raise PublishError('还没有可加入合集的已投稿稿件')
        elif action == 'check' and not record['receipt'].get('bvid'):
            raise PublishError('尚无BV号，请在官方创作中心核对后填写回执')
        if not transport.available():
            raise PublishError('发布组件尚未安装，不能开始上传或投稿')
        cancellation = threading.Event()
        _running[key] = cancellation
        _update(key, status='queued', error='')
        _pool.submit(_execute, key, action, cancellation)
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
    try:
        record = view(key)
        snapshot = record['snapshot']
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
            upload_preflight = not bool(transfers)
            _validate_submission(snapshot, remote('metadata'))
            upload_preflight = False
            _update(key, status='uploading')
            total = sum(p['size'] for p in snapshot['parts'])
            loaded = 0
            for part in snapshot['parts']:
                if cancel.is_set():
                    raise PublishError('操作已暂停')
                path = Path(part['path'])
                if not path.is_file() or path.stat().st_size != part['size'] or _file_hash(path) != part['sha256']:
                    raise PublishError('确认后的成片已改变或丢失，已停止上传')
                part_key = str(part['artifact_id'])
                # 完整分P可恢复；未完成的分P重新上传，不冒称跨进程分片续传。
                if part_key not in transfers:
                    def progress(event):
                        if event.get('event') == 'progress':
                            done = min(part['size'], max(0, float(event.get('loaded', 0))))
                            _update(key, progress=min(99.9, 100 * (loaded + done) / total))
                    transfers[part_key] = remote('upload', part, on_event=progress, timeout=24 * 3600)
                    _update(key, transfers=transfers)
                loaded += part['size']
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
                _update(key, status='uploaded', progress=100)
        if action == 'submit':
            # 首次投稿前重新读取能力；校验失败没有副作用，允许恢复待投稿。
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
                _update(key, status='submitting')
                submit_started = True
            receipt = remote('submit', dict(confirmed=True, settings=snapshot['settings'],
                                            videos=videos, cover=transfers['cover']))
            _update(key, receipt=receipt, status='submitted')
            submit_started = False
            if snapshot['settings']['season_id']:
                action = 'collection'
        if action == 'collection':
            record = view(key)
            receipt = record['receipt']
            _update(key, status='joining')
            status = remote('status', dict(bvid=receipt['bvid']))
            if not status['videos']:
                raise PublishError('B站尚未返回分P信息，请稍后重试加入合集')
            joined = remote('collection', dict(season_id=snapshot['settings']['season_id'],
                section_id=snapshot['settings']['section_id'], aid=receipt['aid'],
                cid=status['videos'][0]['cid'], title=snapshot['settings']['title']))
            if not isinstance(joined, dict) or joined.get('joined') is not True:
                raise PublishError('加入合集回执未确认，请稍后重试核对', code='PUBLISH_COLLECTION_JOIN_UNKNOWN')
            _update(key, receipt={**receipt, 'collection_joined': True}, status='submitted')
        elif action == 'check':
            _update(key, status='checking')
            receipt = record['receipt']
            status = remote('status', dict(bvid=receipt['bvid']))
            state = status.get('state')
            # 合集未完成不能被查询状态覆盖；私密稿件也不能误标为公开发布。
            next_status = ('collection_pending' if snapshot['settings']['season_id'] and not receipt.get('collection_joined')
                           else 'rejected' if state in {-2, -16} else 'reviewed' if state == 0 and snapshot.get('update_target')
                           else 'private' if state == 0 and snapshot['settings']['visibility'] == 'self'
                           else 'published' if state == 0 else 'submitted')
            _update(key, status=next_status,
                    receipt={**receipt, 'remote_state': state, 'state_desc': str(status.get('state_desc', ''))[:200],
                             'reject_reason': str(status.get('reject_reason', ''))[:2000],
                             'videos': [{k: p.get(k) for k in ('cid', 'title', 'state_desc', 'reject_reason', 'xcode_state')}
                                        for p in status.get('videos', [])]})
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
    with _lock, Session(get_engine()) as session:
        row = session.exec(select(Publication).where(Publication.project_uid == uid,
                                                    Publication.status.in_(ACTIVE))).first()
        if row:
            raise PublishError('项目有进行中的发布任务，请先等待或暂停上传后再删除项目')


@contextmanager
def output_replacement(uid, *, deleting=False):
    """提交成片与发布入队互斥，不能在上传读取文件时替换其内容。"""
    with _lock, Session(get_engine()) as session:
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
