"""文件和目录只允许回收，不降级为永久删除；Windows 明确拒绝不可回收操作。"""
import sys
from pathlib import Path

from dubforge.errors import DubForgeError


class RecycleError(DubForgeError):
    code = 'RECYCLE_FAILED'
    suggestion = '请关闭占用文件的程序，并检查磁盘回收站设置后重试；不会改用永久删除。'


def require_recycle(flags):
    if not flags & 0x80:  # TSF_DELETE_RECYCLE_IF_POSSIBLE
        raise RecycleError('此文件或目录无法进入回收站，已取消删除')


def _windows_recycle(path):
    try:
        import pythoncom
        from win32com.shell import shell, shellcon
        from win32com.server.exception import COMException
        from send2trash.win.IFileOperationProgressSink import FileOperationProgressSink
    except ImportError as exc:
        raise RecycleError('安全回收组件未安装，请运行 uv sync 后重试') from exc

    class RecycleOnlySink(FileOperationProgressSink):
        def PreDeleteItem(self, flags, item):
            try:
                require_recycle(flags)
            except RecycleError:
                # pywin32 的返回值不等同失败 HRESULT，必须抛 COM 异常中止操作。
                raise COMException('禁止永久删除', scode=-2147467260)  # E_ABORT
            return 0

    pythoncom.CoInitialize()
    try:
        operation = pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None,
                                               pythoncom.CLSCTX_ALL, shell.IID_IFileOperation)
        # 不遍历关联文件，不显示系统确认/错误框；要求回收、保留撤销并在错误时停止。
        operation.SetOperationFlags(shellcon.FOF_SILENT | shellcon.FOF_NOCONFIRMATION
                                    | shellcon.FOF_NOERRORUI | 0x2000 | 0x00100000
                                    | 0x00080000 | 0x20000000)
        sink = RecycleOnlySink()
        wrapped = pythoncom.WrapObject(sink, shell.IID_IFileOperationProgressSink)
        item = shell.SHCreateItemFromParsingName(str(path), None, shell.IID_IShellItem)
        operation.DeleteItem(item, wrapped)
        result = operation.PerformOperations()
        if result or operation.GetAnyOperationsAborted() or not sink.newItem:
            raise RecycleError('文件未完成回收，请刷新列表后检查状态')
    finally:
        pythoncom.CoUninitialize()


def recycle_file(path: Path):
    try:
        if sys.platform == 'win32':
            _windows_recycle(path)
        else:
            from send2trash import send2trash
            send2trash(str(path))
    except RecycleError:
        raise
    except Exception as exc:
        raise RecycleError('无法移入回收站，请检查文件占用与回收站状态') from exc
    if path.exists():
        raise RecycleError('文件仍在原位置，未确认回收成功，请刷新列表')
