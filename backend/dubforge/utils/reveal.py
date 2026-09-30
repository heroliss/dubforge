"""在 Windows 资源管理器中定位文件/目录 (最朴素的标准做法)
explorer /select,"path" 打开父目录并选中目标。窗口是否前置由系统决定,
不额外干预 (此前尝试过后台线程 SwitchToThisWindow 强制置前, 实测无效, 已移除)。
"""
import subprocess
from pathlib import Path

from dubforge.errors import DubForgeError


class RevealError(DubForgeError):
    code = 'PATH_REVEAL_FAILED'
    suggestion = '请确认文件所在磁盘可访问后重试。'


def reveal_path(path: str) -> dict:
    """只定位绝对本地路径；缺失文件退回最近的现存父目录，不创建目录。"""
    if (not isinstance(path, str) or not path.strip() or any(ord(c) < 32 for c in path)
            or '"' in path or path.startswith(('\\\\', '//'))):
        raise RevealError('请选择有效的本地文件或目录', code='PATH_INVALID')
    target = Path(path)
    if not target.is_absolute():
        raise RevealError('定位文件需要完整的本地路径', code='PATH_INVALID')
    try:
        target = target.resolve()
        missing = not target.exists()
        located = target
        while not located.exists() and located != located.parent:
            located = located.parent
        if not located.exists():
            raise RevealError('文件所在磁盘或目录不可访问', code='PATH_NOT_FOUND')
        reveal_in_explorer(str(located))
        return {'ok': True, 'path': str(located), 'missing': missing,
                'message': '文件已缺失，已打开最近的现存目录' if missing else ''}
    except (OSError, ValueError) as exc:
        raise RevealError('无法在资源管理器中定位，请检查文件路径和磁盘连接') from exc


def reveal_in_explorer(path: str) -> None:
    target = Path(path)
    # 文件只选中、不执行；目录直接进入。参数列表不经过 shell 解释。
    command = ['explorer.exe', str(target)] if target.is_dir() else ['explorer.exe', '/select,', str(target)]
    subprocess.Popen(command)
