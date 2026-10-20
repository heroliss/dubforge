"""试片历史、实际生成快照与可恢复回收；读取不解码、哈希或改写视频。"""
import copy
import datetime as dt
import json
import math
import re
from pathlib import Path
from urllib.parse import quote

from sqlmodel import select

from dubforge.domain import crud
from dubforge.domain.models import Artifact, Job
from dubforge.errors import DubForgeError
from dubforge.execution_progress import _text, normalize
from dubforge.pipeline import assembly_versions as versions
from dubforge.pipeline import output_files
from dubforge.utils.fs import atomic_write_bytes
from dubforge.utils.recycle import recycle_file

PREVIEW_NAME = re.compile(r'preview_version_([a-z0-9][a-z0-9_-]{0,63})_([0-9a-f]{32})\.mp4')
MAX_METADATA_BYTES = 8 * 1024 * 1024


class PreviewError(DubForgeError):
    code = 'ASSEMBLY_PREVIEW_INVALID'
    suggestion = '请刷新试片历史后重试；仅能管理本项目输出目录内的试片。'


def metadata_path(path):
    return Path(path).with_name(Path(path).name + '.preview.json')


def _identity(path):
    info = path.stat()
    return dict(name=path.name, size=info.st_size, mtime_ns=info.st_mtime_ns)


def _safe_values(values):
    if not isinstance(values, dict):
        return {}
    allowed = versions.RENDER_KEYS | {field['path'] for field in versions.fields()}
    allowed |= {'content_review.' + key for key in ('enabled', 'mute_audio', 'audio_mode', 'blocked_terms', 'blocked_ranges')}
    return {key: copy.deepcopy(value) for key, value in values.items() if key in allowed}


def record(ctx, version, path, input_fingerprint, *, start, end):
    """只持久化版本允许的生效值和已冻结方案，不保存服务配置、凭据或提示词。"""
    spec = {key: copy.deepcopy(version[key]) for key in
            ('id', 'name', 'dubbing', 'burn_subtitles', 'include_comments', 'overrides')}
    snapshot = dict(version_spec=spec,
        effective_values=_safe_values(version['values']),
        comment_plan=copy.deepcopy(version.get('comment_overlay')),
        output_cuts=copy.deepcopy(version.get('output_cuts')),
        input_fingerprint=input_fingerprint, render_fingerprint=input_fingerprint,
        plan_fingerprint=versions.digest([dict(id=version['id'], name=version['name'], fingerprint=input_fingerprint)]),
        overlay_revision=ctx.task_data.get('overlay_revision') if version['include_comments'] else None)
    value = dict(schema=1, file=_identity(path), job_id=getattr(ctx, 'job_id', None),
        created_at=dt.datetime.now(dt.timezone.utc).isoformat(), range=dict(start=start, end=end), snapshot=snapshot)
    atomic_write_bytes(metadata_path(path), json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))


def _project(uid):
    project = crud.get_project(uid)
    if project is None:
        raise PreviewError('项目不存在', code='PROJECT_NOT_FOUND')
    return project, crud.project_dir_by_project(project).resolve()


def _version_id(value):
    if value is not None and (not isinstance(value, str) or not versions.ID.fullmatch(value)):
        raise PreviewError('试片版本标识无效')
    return value


def _parse(value):
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError, RecursionError):
        return {}


def _specs(task):
    values = task.get('version_specs')
    return [row for row in values if isinstance(row, dict)] if isinstance(values, list) else []


def _path(root, name):
    if not isinstance(name, str) or not PREVIEW_NAME.fullmatch(name):
        raise PreviewError('仅支持本项目的版本试片，不能操作正式成片或原始资源')
    candidate = root / 'output' / name
    resolved = candidate.resolve()
    if (resolved.parent != root / 'output' or candidate.is_symlink() or candidate.is_junction()
            or candidate.parent.is_symlink() or candidate.parent.is_junction()):
        raise PreviewError('试片路径不安全，已拒绝操作')
    if candidate.exists() and not candidate.is_file():
        raise PreviewError('试片目标不是普通文件')
    return resolved


def media_path(uid, name):
    _, root = _project(uid)
    path = _path(root, name)
    if not path.is_file():
        raise PreviewError('试片已不在磁盘；从系统回收站原位还原后刷新即可', code='ARTIFACT_MISSING')
    return path


def audio_report(uid, name):
    """旧试片即使失去产物登记，也能读取与该实体文件匹配的声音报告。"""
    from dubforge.pipeline.audio_quality import read_report, report_path
    path = media_path(uid, name)
    sidecar = report_path(path)
    try:
        if sidecar.is_symlink() or sidecar.is_junction() or sidecar.stat().st_size > MAX_METADATA_BYTES:
            return None
    except OSError:
        return None
    return read_report(path)


def _disk_revision(path, artifact=None):
    try:
        info = path.stat()
        return versions.digest([artifact.id if artifact else None, info.st_dev, info.st_ino,
                                info.st_size, info.st_mtime_ns, info.st_ctime_ns])
    except OSError:
        return ''


def _sidecar(path):
    sidecar = metadata_path(path)
    try:
        if sidecar.is_symlink() or sidecar.is_junction() or sidecar.stat().st_size > MAX_METADATA_BYTES:
            return None
        data = _parse(sidecar.read_text(encoding='utf-8'))
        if data.get('schema') != 1 or data.get('file') != _identity(path):
            return None
        snapshot = data.get('snapshot')
        if not isinstance(snapshot, dict):
            return None
        spec = versions.validate(snapshot.get('version_spec'))
        if spec['id'] != PREVIEW_NAME.fullmatch(path.name).group(1):
            return None
        region = data.get('range')
        if (not isinstance(data.get('created_at'), str) or not isinstance(snapshot.get('effective_values'), dict)
                or not isinstance(region, dict)
                or any(type(region.get(key)) not in (int, float) or not math.isfinite(region[key]) for key in ('start', 'end'))
                or region['start'] < 0 or region['end'] <= region['start']):
            return None
        dt.datetime.fromisoformat(data['created_at'].replace('Z', '+00:00'))
        return data
    except (OSError, ValueError, TypeError, DubForgeError):
        return None


def _legacy(job, version_id):
    task = _parse(job.task_data) if job else {}
    if task.get('scope') != 'preview':
        return None
    version = next((value for value in _specs(task) if value.get('id') == version_id), None)
    if version is None:
        return None
    try:
        spec = versions.validate({key: version[key] for key in
            ('id', 'name', 'dubbing', 'burn_subtitles', 'include_comments', 'overrides')})
    except (KeyError, DubForgeError):
        return None
    return dict(version_spec=spec, effective_values=_safe_values(version.get('values', {})),
        comment_plan=copy.deepcopy(version.get('comment_overlay')), output_cuts=copy.deepcopy(version.get('output_cuts')),
        input_fingerprint=None, render_fingerprint=None, plan_fingerprint=None,
        overlay_revision=task.get('overlay_revision'))


def _task(job):
    task = _parse(job.task_data)
    specs = _specs(task)
    scope = 'preview' if task.get('scope') == 'preview' else 'render'
    execution = normalize(_parse(job.execution), state=job.status, started_at=job.started_at,
                          finished_at=job.finished_at, owner='step', job_id=job.id)
    error = execution['detail'] or _text(job.error_detail, 1000)
    return dict(job_id=job.id, status=job.status, scope=scope,
        version_ids=[row.get('id') for row in specs], version_names=[row.get('name') for row in specs],
        range=dict(start=task.get('start'), end=task.get('end')) if scope == 'preview' else None,
        execution=execution,
        error_message=error if job.status in {'failed', 'dead', 'cancelled'} else '',
        started_at=_iso(job.started_at), finished_at=_iso(job.finished_at))


def _iso(value):
    return value.replace(tzinfo=value.tzinfo or dt.timezone.utc).isoformat() if value else None


def _latest_preview_job(session, project_id, version_id):
    # 按页寻找最近一次同版本试片，不把全部历史任务及大型冻结评论方案一次载入内存。
    offset = 0
    while True:
        batch = session.exec(select(Job).where(Job.project_id == project_id, Job.step_key == 'assemble')
            .order_by(Job.id.desc()).offset(offset).limit(32)).all()
        for job in batch:
            task = _parse(job.task_data)
            if task.get('scope') == 'preview' and (version_id is None or version_id in
                    [row.get('id') for row in _specs(task)]):
                return job
        if len(batch) < 32:
            return None
        offset += len(batch)


def history(uid, version_id=None):
    """只读目录索引和小快照；旧流程丢失索引的合法试片也可被查看与清理。"""
    version_id = _version_id(version_id)
    project, root = _project(uid)
    with crud.session() as session:
        artifacts = session.exec(select(Artifact).where(Artifact.project_id == project.id,
            Artifact.step_key == 'assemble').order_by(Artifact.id.desc())).all()
        job_ids = {artifact.job_id for artifact in artifacts if artifact.job_id is not None
                   and PREVIEW_NAME.fullmatch(artifact.name)}
        jobs = session.exec(select(Job).where(Job.project_id == project.id,
            Job.step_key == 'assemble', Job.id.in_(job_ids))).all() if job_ids else []
        latest = _latest_preview_job(session, project.id, version_id)
        active = session.exec(select(Job).where(Job.project_id == project.id,
            Job.step_key == 'assemble', Job.status.in_({'queued', 'running'})).order_by(Job.id.desc()).limit(1)).first()
    jobs_by_id = {job.id: job for job in jobs}
    try:
        current_names = {row['id']: row['name'] for row in versions.catalog(uid)['versions']}
    except DubForgeError:
        current_names = {}
    artifacts_by_name = {}
    for artifact in artifacts:
        if PREVIEW_NAME.fullmatch(artifact.name):
            artifacts_by_name.setdefault(artifact.name, artifact)
    names = set(artifacts_by_name)
    output = root / 'output'
    if output.is_dir() and not output.is_symlink() and not output.is_junction():
        names.update(path.name for path in output.iterdir() if PREVIEW_NAME.fullmatch(path.name))
    items = []
    for name in names:
        found = PREVIEW_NAME.fullmatch(name)
        item_version_id = found.group(1)
        if version_id is not None and item_version_id != version_id:
            continue
        artifact = artifacts_by_name.get(name)
        try:
            path = _path(root, name)
            # 伪造或外部登记不能让同名路径绕过项目目录白名单。
            if artifact and output_files._path(artifact, root) != path:
                continue
            exists = path.is_file()
            stat = path.stat() if exists else None
            metadata = _sidecar(path) if exists else None
        except (OSError, ValueError, DubForgeError):
            continue
        job = jobs_by_id.get(artifact.job_id) if artifact else None
        snapshot = metadata.get('snapshot') if metadata else _legacy(job, item_version_id)
        job_id = metadata.get('job_id') if metadata else artifact.job_id if artifact else None
        generated = metadata.get('created_at') if metadata else _iso(artifact.created_at) if artifact else None
        if generated is None and stat:
            generated = dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat()
        task = _parse(job.task_data) if job else {}
        items.append(dict(artifact_id=artifact.id if artifact else None, name=name,
            version_id=item_version_id, version_name=(snapshot or {}).get('version_spec', {}).get('name',
                current_names.get(item_version_id, '历史版本')),
            created_at=generated, size=stat.st_size if stat else 0, exists=exists,
            file_revision=_disk_revision(path, artifact) if exists else '', job_id=job_id,
            range=metadata.get('range') if metadata else dict(start=task['start'], end=task['end'])
                if 'start' in task and 'end' in task else None,
            metadata_status='ready' if metadata else 'legacy' if snapshot else 'unknown', snapshot=snapshot,
            url=f'/api/projects/{quote(uid, safe="")}/assembly-versions/preview-history/{quote(name, safe="")}/raw',
            report_url=f'/api/projects/{quote(uid, safe="")}/assembly-versions/preview-history/{quote(name, safe="")}/audio-report'))
    items.sort(key=lambda row: (row['created_at'] or '', row['name']), reverse=True)
    from dubforge.pipeline.render_resources import inspect_resources
    return dict(schema=1, output_dir=str(output), total_count=sum(row['exists'] for row in items),
        total_bytes=sum(row['size'] for row in items if row['exists']), items=items,
        latest_task=_task(latest) if latest else None, active_task=_task(active) if active else None,
        resources=inspect_resources(output))


def _references(value, artifact_id, path):
    if isinstance(value, dict):
        if type(artifact_id) is int and value.get('artifact_id') == artifact_id:
            return True
        return any(_references(item, artifact_id, path) for item in value.values())
    if isinstance(value, list):
        return any(_references(item, artifact_id, path) for item in value)
    return isinstance(value, str) and value in {str(path), path.name, 'output/' + path.name, 'output\\' + path.name}


def recycle(uid, body):
    """显式确认名单、逐一验证身份后回收；登记和快照保留以支持原位恢复。"""
    if not isinstance(body, dict) or body.get('confirmed') is not True:
        raise PreviewError('请确认要移入系统回收站的历史试片')
    requested = body.get('items')
    version_id = _version_id(body.get('version_id'))
    if (not isinstance(requested, list) or not 1 <= len(requested) <= 200
            or any(not isinstance(row, dict) for row in requested)
            or len({str(row.get('name')) for row in requested}) != len(requested)):
        raise PreviewError('请选择1–200份不重复的历史试片')
    from dubforge.publishing import service as publishing
    from dubforge.publishing.models import Publication, PublicationUpdate
    from dubforge.runner import runner
    with publishing.output_replacement(uid, deleting=True), runner.output_file_edit(uid):
        current = history(uid, version_id)
        indexed = {row['name']: row for row in current['items']}
        protected = {body['keep_name']} if isinstance(body.get('keep_name'), str) else set()
        # 每个版本至少保留最新一份存在的试片，减少清理后误以为生成结果丢失。
        latest_versions = set()
        for item in current['items']:
            if item['exists'] and item['version_id'] not in latest_versions:
                protected.add(item['name']); latest_versions.add(item['version_id'])
        _, root = _project(uid)
        with crud.session() as session:
            publications = session.exec(select(Publication).where(Publication.project_uid == uid)).all()
            updates = session.exec(select(PublicationUpdate).where(PublicationUpdate.project_uid == uid)).all()
        references = [_parse(value) for row in publications for value in (row.draft, row.snapshot)]
        references.extend(_parse(row.plan) for row in updates)
        paths = []
        missing = 0
        for request in requested:
            name = request.get('name')
            path = _path(root, name)
            item = indexed.get(name)
            if (item is None or request.get('artifact_id') != item['artifact_id']
                    or (request.get('artifact_id') is not None and type(request['artifact_id']) is not int)):
                raise PreviewError('试片记录已变化，请刷新列表', code='ASSEMBLY_PREVIEW_CHANGED')
            if not item['exists']:
                missing += 1
                continue
            if name in protected:
                raise PreviewError('请保留正在预览的试片及每个版本最新的一份试片', code='ASSEMBLY_PREVIEW_KEEP_LATEST')
            if not request.get('file_revision') or request['file_revision'] != item['file_revision']:
                raise PreviewError('确认期间试片文件已变化，请刷新后重新确认', code='ASSEMBLY_PREVIEW_CHANGED')
            if any(_references(value, item['artifact_id'], path) for value in references):
                raise PreviewError('这份试片仍被发布资料引用，请先取消该引用', code='ASSEMBLY_PREVIEW_REFERENCED')
            paths.append((path, item['size']))
        recycled = 0
        recycled_bytes = 0
        failures = []
        for path, size in paths:
            try:
                recycle_file(path)
                recycled += 1; recycled_bytes += size
            except DubForgeError as exc:
                failures.append(dict(name=path.name, code=exc.code, message=exc.message))
        return dict(status='partial' if failures else 'recycled', recycled_count=recycled, missing_count=missing,
            failed_count=len(failures), failures=failures,
            recycled_bytes=recycled_bytes, reclaimed_bytes=0, history=history(uid, version_id),
            message=('部分试片回收失败，其余已移入系统回收站；请刷新后重试。' if failures else
                     '历史试片已移入系统回收站；原位还原后刷新可恢复。回收站清空前不会释放磁盘空间。'))
