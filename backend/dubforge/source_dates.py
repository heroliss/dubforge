"""批量检查时补齐来源发布日期；与媒体下载、AI 整理和流程执行相互独立。"""
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

from dubforge import config, video_info
from dubforge.domain import crud
from dubforge.utils.fs import atomic_write_bytes


# 整个进程最多三个提取器；每个请求留出三秒结束子进程及回收管道。
REQUEST_TIMEOUT_SECONDS = 25.0
_CLEANUP_SECONDS = 3.0
_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='source-dates')
_gate = threading.Lock()
_inflight = {}


def _release(key, future):
    with _gate:
        if _inflight.get(key) is future:
            _inflight.pop(key, None)


def _day(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        return (datetime.strptime(value, '%Y%m%d').date() if len(value) == 8
                else date.fromisoformat(value)).isoformat()
    except ValueError:
        return None


def _stamp(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _fields(source):
    source = source if isinstance(source, dict) else {}
    result = {}
    for key in ('upload_date', 'release_date'):
        if value := _day(source.get(key)):
            result[key] = value
    for key in ('timestamp', 'release_timestamp'):
        if (value := _stamp(source.get(key))) is not None:
            try:
                datetime.fromtimestamp(value, timezone.utc)
            except (ValueError, OSError, OverflowError):
                continue
            result[key] = value
    return result


def _source_date(source):
    fields = _fields(source)
    for key in ('upload_date', 'release_date'):
        if key in fields:
            return fields[key]
    for key in ('timestamp', 'release_timestamp'):
        if key in fields:
            return datetime.fromtimestamp(fields[key], timezone.utc).date().isoformat()
    return None


def _source_identity(url):
    """兼容同一 YouTube 视频的短链/规范链接，不把其它视频的旧资料当缓存。"""
    clean = video_info._url(url)
    if clean and video_info._youtube(clean):
        parsed = urlparse(clean)
        parts = parsed.path.strip('/').split('/')
        if parsed.hostname.lower().endswith('youtu.be'):
            ident = parts[0]
        elif len(parts) > 1 and parts[0] in {'shorts', 'embed', 'live'}:
            ident = parts[1]
        else:
            ident = (parse_qs(parsed.query).get('v') or [''])[0]
        if ident:
            return 'youtube:' + ident
    return clean


def _matches_source(info, url):
    source = info.get('source') if isinstance(info, dict) else None
    old_url = source.get('url') if isinstance(source, dict) else None
    # 兼容只存过日期、未存 URL 的旧版资料；明确属于其它地址的资料不可复用。
    return not old_url or _source_identity(old_url) == _source_identity(url)


def read(uid: str) -> str | None:
    """仅读本地资料；日期只能来自平台字段，绝不使用项目创建或导入时间。"""
    project = crud.get_project(uid)
    if not project:
        return None
    info = video_info._read_json(crud.project_dir_by_project(project) / 'artifacts/download/video_info.json')
    if not info or not _matches_source(info, project.source_url):
        return None
    return _source_date(info.get('source'))


def _settings(project):
    settings = config.get('download', {}).copy()
    overrides = crud.project_step_params(project)
    settings.update(overrides.get('download', {}))
    for key in tuple(settings):
        if f'download.{key}' in overrides:
            settings[key] = overrides[f'download.{key}']
    return settings


def _ensure(uid, expected, deadline):
    def check():
        if time.monotonic() >= deadline:
            raise video_info.VideoInfoError('本次发布日期检查已到时限', code='VIDEO_INFO_TIMEOUT')

    try:
        check()
        project = crud.get_project(uid)
        if (not project or (str(crud.project_dir_by_project(project)), project.uid,
                            project.source_url, project.source_kind, project.id) != expected):
            return None
        if current := read(uid):
            return current
        # operation 与流程、Runner、编辑和其它视频资料操作共用准入边界。
        with video_info.operation(uid, 'source_date') as project:
            pdir = crud.project_dir_by_project(project)
            identity = (str(pdir), project.uid, project.source_url, project.source_kind, project.id)
            if identity != expected or project.source_kind not in {'link', 'youtube'}:
                return None
            if not video_info._url(project.source_url):
                return None
            if current := read(uid):
                return current
            path = pdir / 'artifacts/download/video_info.json'
            previous = video_info._read_json(path)
            if ((path.is_file() and previous is None)
                    or (previous and not _matches_source(previous, project.source_url))):
                return None
            check()
            raw = video_info._fetch_source(project.source_url, _settings(project), cancel_check=check)
            check()
            fields = _fields(raw)
            if not _source_date(fields):
                return None
            with video_info.gate:
                check()
                current_project = crud.get_project(uid)
                if (not current_project or not pdir.is_dir()
                        or (str(crud.project_dir_by_project(current_project)), current_project.uid,
                            current_project.source_url, current_project.source_kind, current_project.id) != expected):
                    return None
                previous = video_info._read_json(path)
                if ((path.is_file() and previous is None)
                        or (previous and not _matches_source(previous, project.source_url))):
                    return None
                if previous and (current := _source_date(previous.get('source'))):
                    return current
                if previous:
                    info = dict(previous)
                    source = info.get('source')
                    info['source'] = {**(source if isinstance(source, dict) else {}), **fields}
                else:
                    # 首次采集可建立基础资料；不调用中文整理、不同步项目名称。
                    info = video_info._build_video_info(project.source_kind, video_info._text(raw.get('title'))
                        or project.title, project.source_url, None, raw, summarize=False)
                    info['source'].update(fields)
                backup = path.read_bytes() if path.is_file() else None
                path.parent.mkdir(parents=True, exist_ok=True)
                check()
                atomic_write_bytes(path, json.dumps(info, ensure_ascii=False, indent=1).encode(), create_parent=False)
                try:
                    crud.add_artifact(project.id, 'download', 'json', path.name, path,
                                      str(path.relative_to(pdir)), replace_existing=True)
                except Exception:
                    if backup is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write_bytes(path, backup, create_parent=False)
                    raise
                return _source_date(info.get('source'))
    except Exception:
        # 平台失败、超时、本地来源或已有任务均不能阻断其它项目的批量检查。
        return None


def ensure_many(uids, *, timeout_seconds: float = REQUEST_TIMEOUT_SECONDS) -> dict[str, str | None]:
    """显式批量检查的有界增强；缓存命中不联网，同一来源的并发请求共享采集。"""
    requested = list(dict.fromkeys(uid for uid in uids if isinstance(uid, str) and uid))
    started = time.monotonic()
    timeout = max(0.0, min(float(timeout_seconds), REQUEST_TIMEOUT_SECONDS))
    deadline = started + max(0.0, timeout - min(_CLEANUP_SECONDS, timeout / 5))
    pending = []
    result = {}
    for uid in requested:
        result[uid] = read(uid)
        if result[uid] or time.monotonic() >= deadline:
            continue
        project = crud.get_project(uid)
        if not project or project.source_kind not in {'link', 'youtube'} or not video_info._url(project.source_url):
            continue
        expected = (str(crud.project_dir_by_project(project)), project.uid, project.source_url, project.source_kind, project.id)
        with _gate:
            future = _inflight.get(expected)
            if future is None or future.done():
                future = _pool.submit(_ensure, uid, expected, deadline)
                _inflight[expected] = future
        future.add_done_callback(lambda completed, key=expected: _release(key, completed))
        pending.append((uid, expected, future))
    if pending:
        wait({future for _, _, future in pending}, timeout=max(0., started + timeout - time.monotonic()))
    for uid, _, future in pending:
        if future.done():
            if not future.cancelled():
                result[uid] = future.result()
        else:
            # 未启动的工作不再排队；运行中的提取器会在截止检查中自行回收。
            future.cancel()
    return result
