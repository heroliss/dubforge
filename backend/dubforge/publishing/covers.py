"""上传格式适配：原封面用于预览，WebP派生PNG仅用于B站上传。"""
import hashlib
import subprocess
import threading
from pathlib import Path

from dubforge import config
from dubforge.errors import DubForgeError
from dubforge.platform_cover import MAX_BYTES, image_extension
from dubforge.utils.fs import atomic_write_bytes

_locks = [threading.Lock() for _ in range(16)]


class UploadCoverError(DubForgeError):
    code = 'PUBLISH_COVER_FORMAT'
    suggestion = '请检查 FFmpeg 是否可用，或改用 JPEG、PNG 封面；原图片不会被覆盖。'


def upload_path(source: Path) -> Path:
    """SDK会把WebP标为octet-stream；派生图按输入哈希缓存，不修改原图。"""
    source = Path(source)
    try:
        if not 0 < source.stat().st_size <= MAX_BYTES:
            raise UploadCoverError('封面为空或超过10MB，请重新选择')
        data = source.read_bytes()
    except OSError:
        raise UploadCoverError('上传封面文件无法读取，请重新选择') from None
    extension = image_extension(data)
    if extension not in {'jpg', 'png', 'webp'} or source.suffix.lower() not in {'.' + extension, '.jpeg' if extension == 'jpg' else ''}:
        raise UploadCoverError('封面内容与格式不匹配，请重新选择图片')
    if extension != 'webp':
        return source
    key = hashlib.sha256(data).hexdigest()
    target = config.data_root() / 'publishing/upload-covers' / f'webp-png-v1-{key}.png'
    with _locks[int(key[:2], 16) % len(_locks)]:
        if target.is_file() and 0 < target.stat().st_size <= MAX_BYTES:
            cached = target.read_bytes()
            if image_extension(cached) == 'png' and cached.endswith(b'IEND\xaeB`\x82'):
                return target
        # 单帧、限时、限制长边；只读本地图片。输出经校验后原子发布。
        try:
            result = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-max_alloc', '134217728', '-threads', '1', '-protocol_whitelist', 'file,pipe',
                '-i', str(source), '-frames:v', '1', '-filter_threads', '1',
                '-vf', 'scale=w=min(2048\\,iw):h=min(2048\\,ih):force_original_aspect_ratio=decrease',
                '-c:v', 'png', '-threads', '1', '-f', 'image2pipe', 'pipe:1'],
                capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            raise UploadCoverError('WebP封面转换失败或超时，未上传封面，请检查FFmpeg') from None
        if (result.returncode or not 0 < len(result.stdout) <= MAX_BYTES
                or image_extension(result.stdout) != 'png' or not result.stdout.endswith(b'IEND\xaeB`\x82')):
            raise UploadCoverError('WebP封面无法转换为有效PNG，未上传封面，请重新选择图片')
        try:
            atomic_write_bytes(target, result.stdout)
        except OSError:
            raise UploadCoverError('兼容封面无法保存，请检查磁盘空间和目录权限') from None
        return target
