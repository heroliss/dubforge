"""平台封面预览与发布共享缓存，不写制作步骤或修改稿件。"""
import hashlib
import threading
from urllib.parse import urlparse

import httpx

from dubforge import config, video_info
from dubforge.errors import DubForgeError
from dubforge.utils.fs import atomic_write_bytes

MAX_BYTES = 10 * 1024 * 1024
_locks = [threading.Lock() for _ in range(16)]


class PlatformCoverError(DubForgeError):
    code = 'PLATFORM_COVER_FAILED'
    suggestion = '请重试或手动上传封面；原有稿件封面不会被清除。'


def image_extension(data):
    return ('png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'jpg' if data.startswith(b'\xff\xd8\xff')
            else 'webp' if data[:4] == b'RIFF' and data[8:12] == b'WEBP' else None)


def supported(url):
    try:
        parsed = urlparse(url)
        return (parsed.scheme == 'https' and parsed.hostname in {'i.ytimg.com', 'img.youtube.com'}
                and parsed.port in {None, 443} and not parsed.username and not parsed.password)
    except (TypeError, ValueError):
        return False


def get(uid):
    info = video_info.view(uid).get('info') or {}
    url = (info.get('content') or {}).get('thumbnail', '')
    if not supported(url):
        raise PlatformCoverError('此平台封面不能自动导入，请手动上传图片')
    # URL 改变即使用新缓存；固定数量的锁合并同一封面并发预览/导入请求。
    key = hashlib.sha256(url.encode()).hexdigest()
    root = config.data_root() / 'cache/platform-covers'
    with _locks[int(key[:2], 16) % len(_locks)]:
        for extension in ('png', 'jpg', 'webp'):
            path = root / f'{key}.{extension}'
            if path.is_file() and 0 < path.stat().st_size <= MAX_BYTES:
                data = path.read_bytes()
                if image_extension(data) == extension:
                    return path
        try:
            with httpx.stream('GET', url, follow_redirects=False, timeout=30,
                              proxy=config.get('download.proxy') or None) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise PlatformCoverError('平台封面超过10MB，请手动选择较小图片')
            extension = image_extension(data)
            if not extension:
                raise PlatformCoverError('平台未返回可用图片，请重试或手动上传')
            path = root / f'{key}.{extension}'
            atomic_write_bytes(path, bytes(data))
            return path
        except httpx.HTTPError:
            raise PlatformCoverError('平台封面下载失败，请重试或手动上传') from None
