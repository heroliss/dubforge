# -*- coding: utf-8 -*-
"""在 Windows 资源管理器中定位文件/目录 (最朴素的标准做法)
explorer /select,"path" 打开父目录并选中目标。窗口是否前置由系统决定,
不额外干预 (此前尝试过后台线程 SwitchToThisWindow 强制置前, 实测无效, 已移除)。
"""
import subprocess


def reveal_in_explorer(path: str) -> None:
    subprocess.Popen(f'explorer /select,"{path}"')
