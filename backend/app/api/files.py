# -*- coding: utf-8 -*-
"""本地文件 API: 目录浏览 (服务端即本机, 浏览服务端目录即浏览用户磁盘)"""
import string
from pathlib import Path

from fastapi import APIRouter

from dubforge import config
from dubforge.errors import DubForgeError

router = APIRouter(prefix="/api/files", tags=["files"])

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv",
              ".wmv", ".ts", ".m4v", ".mpg", ".mpeg"}


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
