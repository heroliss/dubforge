"""成片编码的保守资源边界；不调整系统、外部服务或成片画质。"""
import ctypes
import errno
import os
import shutil
import tempfile
from pathlib import Path

import psutil

from dubforge.errors import DubForgeError
from dubforge.execution_progress import report_stage

_MIB = 1024 ** 2
_GIB = 1024 ** 3
DECODE_THREADS = 2
FILTER_THREADS = 2
ENCODE_THREADS = 4


class RenderResourceError(DubForgeError):
    """磁盘或系统提交内存不足，保留旧成品并给出可操作建议。"""


def _commit_memory():
    """Windows 的可提交余量包含分页文件，与剩余物理内存不是同一个量。"""
    if os.name != 'nt':
        return None, None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong),
                   ('physical_total', ctypes.c_ulonglong), ('physical_available', ctypes.c_ulonglong),
                   ('commit_limit', ctypes.c_ulonglong), ('commit_available', ctypes.c_ulonglong),
                   ('virtual_total', ctypes.c_ulonglong), ('virtual_available', ctypes.c_ulonglong),
                   ('extended_available', ctypes.c_ulonglong)]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    try:
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.commit_available), int(status.commit_limit)
    except (OSError, AttributeError):
        pass
    return None, None


def inspect_resources(output_dir):
    """只读快照；探测失败保留未知值，不能以未知值阻止用户运行。"""
    output = Path(output_dir).resolve()
    existing = output
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    def free(path):
        try:
            return shutil.disk_usage(path).free if path else None
        except OSError:
            return None
    disk_free = free(existing)
    system = (Path(os.environ.get('SystemRoot', 'C:\\Windows')).anchor if os.name == 'nt' else '/')
    try:
        temporary = tempfile.gettempdir()
    except (OSError, FileNotFoundError):
        temporary = None
    try:
        physical_available = int(psutil.virtual_memory().available)
    except (OSError, psutil.Error):
        physical_available = None
    available, limit = _commit_memory()
    return dict(output_directory=str(output), disk_free_bytes=disk_free,
                system_directory=system, system_disk_free_bytes=free(system),
                temp_directory=temporary, temp_disk_free_bytes=free(temporary),
                physical_available_bytes=physical_available,
                commit_available_bytes=available, commit_limit_bytes=limit,
                decode_threads=DECODE_THREADS, filter_threads=FILTER_THREADS,
                encode_threads=ENCODE_THREADS)


def _amount(value):
    return '未知' if value is None else f'{value / _GIB:.2f} GiB'


def preflight_render(ctx, output_dir, *, required_bytes=0):
    """仅对已知写入下限和几乎耗尽的提交内存硬阻断，其余情况提示。"""
    check = getattr(ctx, 'check_cancel', None)
    if callable(check):
        check()
    snapshot = inspect_resources(output_dir)
    disk = snapshot['disk_free_bytes']
    commit = snapshot['commit_available_bytes']
    # 不猜测压缩视频体积。已知的整轨 WAV 则能计算最低所需空间。
    minimum_disk = max(8 * _MIB, int(required_bytes))
    if disk is not None and disk < minimum_disk:
        raise RenderResourceError(
            f'合成目录磁盘空间不足：可用 {_amount(disk)}，本阶段至少需要 {_amount(minimum_disk)}',
            code='ASSEMBLE_DISK_SPACE',
            suggestion='请在合成目录所在磁盘清理不再需要的历史试片或缓存后重试；旧成品保留。', detail=snapshot)
    if commit is not None and commit < 64 * _MIB:
        raise RenderResourceError(
            '系统可提交内存几乎耗尽，无法安全开始成片编码', code='ASSEMBLE_MEMORY_PRESSURE',
            suggestion='请保存工作，关闭暂时不用的配音服务、Unity 等高占用程序；检查分页文件所在磁盘空间后重试。',
            detail=snapshot)
    pressure = (commit is not None and commit < max(2 * _GIB, (snapshot['commit_limit_bytes'] or 0) * .08)
                or snapshot['physical_available_bytes'] is not None
                and snapshot['physical_available_bytes'] < 512 * _MIB)
    system_low = snapshot.get('system_disk_free_bytes') is not None and snapshot['system_disk_free_bytes'] < _GIB
    temp_low = snapshot.get('temp_disk_free_bytes') is not None and snapshot['temp_disk_free_bytes'] < _GIB
    text = (f'合成目录（{snapshot["output_directory"]}）可用 {_amount(disk)}；'
            f'系统盘（{snapshot.get("system_directory") or "未知"}）可用 {_amount(snapshot.get("system_disk_free_bytes"))}；'
            f'系统临时目录（{snapshot.get("temp_directory") or "未知"}）可用 {_amount(snapshot.get("temp_disk_free_bytes"))}；'
            f'物理内存可用 {_amount(snapshot["physical_available_bytes"])}；'
            f'系统可提交余量 {_amount(commit)}；解码/滤镜/编码线程上限 '
            f'{DECODE_THREADS}/{FILTER_THREADS}/{ENCODE_THREADS}')
    if pressure:
        text += '。内存余量偏低，建议关闭暂时不用的配音服务或其他大型程序。'
    if system_low or temp_low:
        text += '。系统盘或系统临时目录空间偏低，请清理缓存；这与合成目录空间不同，不据此阻止本次合成。'
    log = getattr(ctx, 'log', None)
    if callable(log):
        log(('⚠ ' if pressure or system_low or temp_low else '') + text)
    report_stage(ctx, 'checking_render_resources', '检查合成磁盘与系统内存', detail=text)
    return snapshot


def bounded_ffmpeg_command(cmd):
    """输入/滤镜/输出分别设上限，避免多核自动线程成倍扩大 4K 缓冲。"""
    command = [str(part) for part in cmd]
    if not command or Path(command[0]).stem.lower() != 'ffmpeg':
        return command
    for index, part in enumerate(command[:-1]):
        maximum = {'-filter_threads': FILTER_THREADS, '-filter_complex_threads': FILTER_THREADS}.get(part)
        if maximum is not None:
            try:
                command[index + 1] = str(max(1, min(maximum, int(command[index + 1]))))
            except (ValueError, TypeError):
                command[index + 1] = str(maximum)
    globals_ = []
    for option in ('-filter_threads', '-filter_complex_threads'):
        if option not in command:
            globals_.extend([option, str(FILTER_THREADS)])
    command[1:1] = globals_
    # 每个 -i 前的选项作用于该输入；输出编码线程必须在最后一个输入之后。
    limited = []
    for part in command:
        if part == '-i':
            limited.extend(['-threads:v', str(DECODE_THREADS)])
        limited.append(part)
    limited[-1:-1] = ['-threads:v', str(ENCODE_THREADS)]
    return limited


def ffmpeg_failure(error, *, output_dir=None, fallback_code='FFMPEG_FAIL',
                   message='成片封装失败，请检查 FFmpeg 日志'):
    """保留实际日志，按证据归类；不能把一般编码失败都归因为资源问题。"""
    detail = error.decode('utf-8', 'replace') if isinstance(error, bytes) else str(error)
    lower = detail.lower()
    disk_error = (isinstance(error, OSError) and (error.errno == errno.ENOSPC or getattr(error, 'winerror', None) in {39, 112})
                  or any(text in lower for text in ('no space left on device', 'disk full', 'not enough space on the disk')))
    memory_error = (isinstance(error, MemoryError)
                    or isinstance(error, OSError) and (error.errno == errno.ENOMEM or getattr(error, 'winerror', None) in {8, 14, 1450, 1455})
                    or any(text in lower for text in ('cannot allocate memory', 'out of memory', 'bad_alloc',
                        'malloc of size', 'memory allocation failed', 'failed to allocate memory',
                        'could not allocate memory', 'not enough memory', 'paging file is too small')))
    snapshot = inspect_resources(output_dir) if output_dir is not None else None
    evidence = dict(log=detail[-2500:], resources=snapshot)
    if disk_error:
        return RenderResourceError('合成写入失败：磁盘空间不足，旧成品保留', code='ASSEMBLE_DISK_SPACE',
            suggestion='请检查合成目录及系统临时目录所在磁盘，清理不再需要的试片或缓存后重试。', detail=evidence)
    if memory_error:
        return RenderResourceError('成片编码内存分配失败，旧成品保留', code='ASSEMBLE_MEMORY_PRESSURE',
            suggestion='请关闭暂时不用的配音服务、Unity 等高占用程序，并确认分页文件所在磁盘有空间；无需重新配音，释放内存后重试合成即可。',
            detail=evidence)
    return DubForgeError(message, code=fallback_code, detail=detail[-2500:])
