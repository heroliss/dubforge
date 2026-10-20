"""项目自动名称：复用资料缓存，不联网、不生成文案、不移动项目目录。"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from sqlalchemy import update

from dubforge.domain import crud
from dubforge.domain.models import Project

_MAX_CACHE_BYTES = 2 * 1024 * 1024
_YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com',
                  'music.youtube.com', 'youtu.be', 'www.youtu.be'}
_LOGGER = logging.getLogger(__name__)


def _title(value) -> str:
    """名称只接受文字；换行和控制字符不进入列表或清单。"""
    if not isinstance(value, str):
        return ''
    value = re.sub(r'[\x00-\x1f\x7f]', ' ', value).lstrip('\ufeff')
    return ' '.join(value.split())[:200]


def _youtube_id(parts) -> str:
    host = (parts.hostname or '').lower()
    if host not in _YOUTUBE_HOSTS:
        return ''
    segments = parts.path.strip('/').split('/')
    if host.endswith('youtu.be'):
        return segments[0]
    return (parse_qs(parts.query).get('v', [''])[0]
            or (segments[1] if len(segments) >= 2 and segments[0] in {'shorts', 'embed', 'live'} else ''))


def placeholder(source_url: str, source_kind: str) -> str:
    """仅从来源生成可读占位名，链接中的凭据不会进入名称。"""
    source = source_url.strip().strip('"') if isinstance(source_url, str) else ''
    if source_kind == 'local':
        path = PureWindowsPath(source) if '\\' in source or re.match(r'^[A-Za-z]:', source) else Path(source)
        return _title(path.stem) or '本地视频'
    try:
        parts = urlsplit(source)
        host = (parts.hostname or '').lower()
        if host in _YOUTUBE_HOSTS:
            video_id = _title(_youtube_id(parts))
            return 'YouTube 视频' + (f' · {video_id}' if video_id else '')
        tail = _title(unquote(parts.path.rstrip('/').rsplit('/', 1)[-1]))
        return _title(host + (f' · {tail}' if tail else '')) or '链接视频'
    except ValueError:
        return '链接视频'


def best_title(info: dict | None, fallback: str) -> str:
    """中文资料仍有效时优先中文；概要失败或过期时采用平台原始标题。"""
    info = info if isinstance(info, dict) else {}
    if info.get('summary_status') == 'generated' and not info.get('summary_stale'):
        chinese = _title(info.get('title_zh'))
        if chinese:
            return chinese
    return _title(info.get('title_original')) or _title(fallback)


def _source_identity(source, kind):
    if kind == 'local':
        return os.path.normcase(os.path.normpath(str(source).strip().strip('"')))
    parts = urlsplit(str(source).strip())
    youtube_id = _youtube_id(parts)
    if youtube_id:
        return 'youtube:' + youtube_id
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ''))


def _matches_source(project, info) -> bool:
    source = info.get('source') if isinstance(info, dict) else None
    recorded = source.get('url') if isinstance(source, dict) else None
    # 旧缓存未记录 URL 时仍可读；已记录的不同来源不能冒充当前视频。
    if recorded in (None, ''):
        return True
    if not isinstance(recorded, str):
        return False
    try:
        return _source_identity(recorded, project.source_kind) == _source_identity(project.source_url, project.source_kind)
    except ValueError:
        return False


def _read_cache(path: Path) -> dict | None:
    """资料读取有界，坏缓存不影响改名，也不误读大型媒体文件。"""
    try:
        if path.stat().st_size > _MAX_CACHE_BYTES:
            return None
        with path.open('rb') as handle:
            raw = handle.read(_MAX_CACHE_BYTES + 1)
        if len(raw) > _MAX_CACHE_BYTES:
            return None
        value = json.loads(raw.decode('utf-8-sig'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def cached_title(project) -> str:
    """恢复自动命名时读取现有资料；没有资料则用来源占位，不沿用手动名。"""
    fallback = placeholder(project.source_url, project.source_kind)
    root = crud.project_dir_by_project(project)
    info = _read_cache(root / 'artifacts/download/video_info.json')
    if info is not None:
        if not _matches_source(project, info):
            return fallback
        title = best_title(info, '')
        if title:
            return title
    meta = _read_cache(root / 'media/meta.json') or {}
    return _title(meta.get('title')) or fallback


def sync(project, info: dict) -> bool:
    """资料保存后更新自动名称；条件更新防止迟到资料覆盖手动改名或新来源。"""
    if getattr(project, 'title_mode', 'manual') != 'auto' or not _matches_source(project, info):
        return False
    # 没有实际资料标题时不回写运行旧快照中的占位名。
    title = best_title(info, '')
    if not title:
        return False
    with crud.session() as session:
        result = session.exec(update(Project).where(
            Project.uid == project.uid,
            Project.source_url == project.source_url,
            Project.title_mode == 'auto',
            Project.title != title,
        ).values(title=title, updated_at=datetime.now(timezone.utc)))
        changed = result.rowcount == 1
        session.commit()
    if changed:
        try:
            crud.write_manifest(project.uid)
        except Exception:
            # 辅助名称已提交；清单失败不能把完整下载误报为失败。
            _LOGGER.exception('项目自动名称已更新，但备份清单更新失败')
    return changed
