# -*- coding: utf-8 -*-
"""源视频定位 (全管线单一出口)
==============================
本地导入: 原文件就地引用 (零拷贝) —— 几个 GB 的视频复制一遍既费时又费盘,
source_url 即视频本体, 项目目录里只登记引用不落副本。
下载来源: media/video.<ext>。
下游 (preprocess/assemble/workbench) 一律从这里解析, 不各自 glob。
"""
from pathlib import Path


def resolve_video(project, pdir: Path) -> Path | None:
    """定位项目的源视频; 找不到返回 None (调用方决定报什么错)"""
    if project.source_kind == "local" and project.source_url:
        p = Path(project.source_url)
        if p.exists():
            return p
    # 下载来源; 兼容旧项目 (本地导入曾复制进 media/) 或源文件被移走的项目
    media = Path(pdir) / "media"
    for pat in ("video.mp4", "video.mkv", "video.webm", "video.*"):
        hits = sorted(media.glob(pat))
        if hits:
            return hits[0]
    return None
