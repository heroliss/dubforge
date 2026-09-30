"""文件系统原子写工具
====================
Windows 上对刚创建的文件做 os.replace 偶发 WinError 5 (拒绝访问) ——
Defender/搜索索引器会短暂持有新文件的句柄。原子改名必须重试, 否则一次
瞬时锁就把整个步骤打成失败。"""
import os
import tempfile
import time
from pathlib import Path


def atomic_replace(src: "Path | str", dst: "Path | str",
                   attempts: int = 6) -> None:
    """os.replace + 瞬时锁重试 (0.05s 指数退避, 最多 ~1.55s)"""
    src, dst = Path(src), Path(dst)
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (2 ** i))


def atomic_write_bytes(dst: "Path | str", data: bytes, *, create_parent: bool = True) -> None:
    """临时名写入 → 原子改名 (中断不留半成品)"""
    dst = Path(dst)
    if create_parent:
        dst.parent.mkdir(parents=True, exist_ok=True)
    # 每次写入独占临时文件；同名产物并发保存时不能共享或删除对方的临时文件。
    handle = tempfile.NamedTemporaryFile(
        dir=dst.parent, prefix=".dubforge-", suffix=".tmp", delete=False)
    tmp = Path(handle.name)
    try:
        with handle:
            handle.write(data)
        atomic_replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
