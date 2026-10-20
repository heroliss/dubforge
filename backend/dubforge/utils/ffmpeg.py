"""FFmpeg 长滤镜使用临时脚本，避开 Windows 进程命令行长度限制。"""
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

from dubforge.errors import DubForgeError

_SCRIPT_THRESHOLD = 24000


def _extended_windows_path(value):
    """只规范已解析的本机/UNC绝对路径，不访问网络或重复添加扩展前缀。"""
    path = PureWindowsPath(value)
    raw = str(path)
    if not path.is_absolute() or raw.startswith('\\\\.\\'):
        raise DubForgeError('字幕临时文件路径无效，请重试生成', code='SUBTITLE_FILE_INVALID')
    if raw.startswith('\\\\?\\'):
        return raw
    return '\\\\?\\UNC\\' + raw[2:] if raw.startswith('\\\\') else '\\\\?\\' + raw


def _filter_filename(value):
    # 第一层是滤镜选项值，第二层是滤镜图语法；它们都不同于 CLI argv / shell 转义。
    option = value.replace('\\', '\\\\').replace("'", "\\'").replace(':', '\\:')
    graph = option.replace('\\', '\\\\').replace("'", "\\'")
    for token in '[],;':
        graph = graph.replace(token, '\\' + token)
    return graph


def ass_file_filter(path):
    """消费本轮写出的内部 ASS；保持同一 libass 读取器，兼容 Windows 长路径。"""
    path = Path(path)
    if path.suffix != '.ass' or not re.fullmatch(r'[A-Za-z0-9_.-]+', path.name):
        raise DubForgeError('字幕临时文件名无效，请重试生成', code='SUBTITLE_FILE_INVALID')
    if os.name == 'nt':
        return 'ass=filename=' + _filter_filename(_extended_windows_path(path.resolve()))
    return 'ass=' + path.name


@contextmanager
def complex_filter_command(cmd, cwd):
    """返回可执行命令；调用方须在上下文内等待 FFmpeg 退出。"""
    prepared = [str(item) for item in cmd]
    # Windows 计算 UTF-16 单元数；非 BMP 字符不能只按 Python 字符数计算。
    length = len(subprocess.list2cmdline(prepared).encode('utf-16-le')) // 2
    indices = [index for index, arg in enumerate(prepared[:-1]) if arg == '-filter_complex']
    if length < _SCRIPT_THRESHOLD or not indices:
        yield prepared
        return
    scripts = []
    try:
        for index in indices:
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                        dir=cwd, prefix='.dubforge-filter-', suffix='.ffscript', delete=False) as handle:
                    path = Path(handle.name)
                    scripts.append(path)
                    handle.write(prepared[index + 1])
                prepared[index:index + 2] = ['-filter_complex_script', str(path)]
            except OSError as exc:
                raise DubForgeError('无法写入 FFmpeg 临时滤镜脚本，请检查合成目录的空间和权限后重试',
                                   code='FFMPEG_FILTER_SCRIPT_WRITE_FAILED') from exc
        yield prepared
    finally:
        for path in scripts:
            path.unlink(missing_ok=True)
