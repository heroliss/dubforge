"""本地文件 API: 目录浏览 (服务端即本机, 浏览服务端目录即浏览用户磁盘)"""
import string
import stat
from os import stat_result
from pathlib import Path
from urllib.parse import urlencode, urlparse

from app.media import MediaFileResponse
from dubforge.errors import DubForgeError
from fastapi import APIRouter, Request, Response

router = APIRouter(prefix="/api/files", tags=["files"])


def _require_local_page(request: Request) -> None:
    """本机网页可读取参考音频；禁止其他站点借浏览器访问本机文件。"""
    origin = request.headers.get('origin')
    try:
        origin_host = urlparse(origin).hostname if origin else None
    except ValueError:
        origin_host = None
    if (request.headers.get('sec-fetch-site') == 'cross-site'
            or origin and origin_host not in {
                request.url.hostname, 'localhost', '127.0.0.1', '::1'}):
        raise DubForgeError('请在 DubForge 页面内访问本机文件', code='FILE_ORIGIN')


@router.post('/reveal')
def reveal_file(body: dict, request: Request, response: Response):
    from dubforge.utils.reveal import reveal_path
    _require_local_page(request)
    response.headers['Cache-Control'] = 'no-store'
    return reveal_path(body.get('path'))

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv",
              ".wmv", ".ts", ".m4v", ".mpg", ".mpeg"}

REFERENCE_AUDIO_TYPES = {
    '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.flac': 'audio/flac',
    '.ogg': 'audio/ogg', '.oga': 'audio/ogg', '.opus': 'audio/ogg',
    '.m4a': 'audio/mp4', '.aac': 'audio/aac', '.wma': 'audio/x-ms-wma',
    '.aiff': 'audio/aiff', '.aif': 'audio/aiff',
}


def _reference_audio_path(value: str) -> tuple[Path, stat_result]:
    """只接受本机普通音频文件；不解释 URL、设备路径或网络共享。"""
    value = value.strip()
    normalized = value.replace('\\', '/')
    path = Path(value)
    if (not value or any(ord(char) < 32 for char in value)
            or normalized.startswith(('//', '/??/')) or '://' in value
            or not path.is_absolute() or ':' in value[len(path.drive):]):
        raise DubForgeError('请选择本机音频文件的完整路径', code='REFERENCE_FILE_INVALID',
                            suggestion='支持 WAV、MP3、FLAC 等音频文件；不能使用网址或网络共享。')
    if path.suffix.lower() not in REFERENCE_AUDIO_TYPES:
        raise DubForgeError('该文件不是支持的参考音频格式', code='REFERENCE_FILE_FORMAT',
                            suggestion='请选择 WAV、MP3、FLAC 等音频；实际播放能力取决于浏览器。')
    try:
        path = path.resolve(strict=True)
        if str(path).replace('\\', '/').startswith(('//', '/??/')):
            raise DubForgeError('参考音频必须位于本机磁盘', code='REFERENCE_FILE_INVALID')
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise DubForgeError('参考音频路径不是普通文件', code='REFERENCE_FILE_INVALID')
        if path.suffix.lower() not in REFERENCE_AUDIO_TYPES:
            raise DubForgeError('该文件不是支持的参考音频格式', code='REFERENCE_FILE_FORMAT')
    except FileNotFoundError as exc:
        raise DubForgeError('参考音频不存在或已经移走', code='REFERENCE_FILE_NOT_FOUND',
                            suggestion='请重新选择参考音频文件。') from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise DubForgeError('无法读取参考音频文件', code='REFERENCE_FILE_UNREADABLE',
                            suggestion='请确认文件可访问，且没有被其他程序独占。') from exc
    return path, info


@router.get('/reference-audio')
def reference_audio(path: str, request: Request, response: Response):
    """试听当前表单选择的本机参考文件，不写入设置或制作产物。"""
    _require_local_page(request)
    resolved, info = _reference_audio_path(path)
    response.headers['Cache-Control'] = 'no-store'
    query = urlencode({'path': str(resolved), 'v': f'{info.st_mtime_ns}-{info.st_size}'})
    return {'url': f'/api/files/reference-audio/content?{query}', 'filename': resolved.name}


@router.get('/reference-audio/content')
@router.head('/reference-audio/content', include_in_schema=False)
def reference_audio_content(path: str, request: Request):
    """原文件按 Range 流式播放，不重新切片、合成或加载完整音频到内存。"""
    _require_local_page(request)
    resolved, info = _reference_audio_path(path)
    return MediaFileResponse(resolved, media_type=REFERENCE_AUDIO_TYPES[resolved.suffix.lower()],
                             stat_result=info, headers={'Cache-Control': 'no-store',
                                                       'X-Content-Type-Options': 'nosniff'})


def _drives() -> list[dict]:
    """Windows 盘符列表 (path 为空时展示)"""
    return [{"name": f"{letter}:\\  (磁盘)", "path": f"{letter}:\\", "is_dir": True,
             "size": 0, "video": False}
            for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]


def _entry(child: Path) -> dict | None:
    try:
        st = child.stat()
        return {"name": child.name, "path": str(child), "is_dir": child.is_dir(),
                "size": st.st_size, "video": child.suffix.lower() in VIDEO_EXTS}
    except OSError:
        return None        # 无权限/被占用的项跳过


@router.get("/list")
def list_dir(path: str = ""):
    """列出文件夹内容; path 为空 → 盘符列表"""
    if not path.strip():
        return {"path": "", "parent": None, "entries": _drives(), "drives": True}
    p = Path(path).resolve()
    if not p.exists():
        raise DubForgeError(f"路径不存在: {p}", code="PATH_NOT_FOUND",
                            suggestion="请确认盘符与文件夹名。")
    if not p.is_dir():
        raise DubForgeError(f"不是文件夹: {p}", code="NOT_A_DIR")
    entries = []
    try:
        children = sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))
        entries = [e for e in (_entry(c) for c in children) if e]
    except PermissionError:
        pass               # 无权限目录显示为空列表
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None,
            "entries": entries, "drives": False}
