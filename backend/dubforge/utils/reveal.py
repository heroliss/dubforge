# -*- coding: utf-8 -*-
"""在 Windows 资源管理器中定位文件/目录, 并尽量把窗口推到前台
==============================================================
explorer /select,"path" 打开父目录并选中目标; 但 Windows 的防抢焦点机制
常把新窗口压在已有窗口后面 —— 这里在后台线程轮询找到新开的资源管理器
窗口 (CabinetWClass), 用 SwitchToThisWindow 强制置前。失败不影响调用方。
"""
import os
import subprocess
import threading


def reveal_in_explorer(path: str) -> None:
    """定位 path (选中它); 立即返回, 置前动作在后台线程完成"""
    subprocess.Popen(f'explorer /select,"{path}"')
    threading.Thread(target=_foreground_new_window,
                     args=(path, _cabinet_windows()), daemon=True).start()


def _cabinet_windows() -> set:
    """当前可见的资源管理器窗口句柄集合 (CabinetWClass)"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found: set = set()

    def _cb(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value == "CabinetWClass" and user32.IsWindowVisible(hwnd):
            found.add(hwnd)
        return 1

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found


def _foreground_new_window(path: str, before: set) -> None:
    import ctypes
    user32 = ctypes.windll.user32
    folder = os.path.basename(path.rstrip("\\/"))
    for _ in range(16):                       # 最多等 ~4s 等窗口出现
        import time
        time.sleep(0.25)
        now = _cabinet_windows()
        fresh = now - before
        hwnd = None
        if fresh:
            hwnd = sorted(fresh)[-1]          # 新开的窗口
        else:
            for h in now:                     # 复用已有窗口的情况: 按标题匹配
                buf = ctypes.create_unicode_buffer(260)
                user32.GetWindowTextW(h, buf, 260)
                if folder.lower() in buf.value.lower():
                    hwnd = h
                    break
        if hwnd:
            user32.ShowWindow(hwnd, 9)        # SW_RESTORE (最小化则还原)
            user32.SwitchToThisWindow(hwnd, True)
            return
