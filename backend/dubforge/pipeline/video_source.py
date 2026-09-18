# -*- coding: utf-8 -*-
"""源视频定位 (全管线单一出口)
==============================
本地导入有两种形态, source_url 双制式:
  外部引用 (浏览选择/粘贴路径): 绝对路径, 原文件就地引用零拷贝, 项目目录只登记不落副本。
  拖入导入: 浏览器拿不到拖入文件的真实磁盘路径, 只能传内容 —— 副本存进项目目录 media/,
  source_url 记项目内相对路径, 是项目自有文件, 随项目存亡。
下载来源: media/video.<ext>。
下游 (preprocess/assemble/workbench) 一律从这里解析, 不各自 glob。
"""
from pathlib import Path


def resolve_source(project, pdir: Path) -> Path | None:
    """把 source_url 解析为绝对路径 (相对路径 = 项目目录内副本); 文件不存在返回 None"""
    if project.source_kind != "local" or not project.source_url:
        return None
    p = Path(project.source_url)
    if not p.is_absolute():
        p = Path(pdir) / p
    return p if p.exists() else None


def resolve_video(project, pdir: Path) -> Path | None:
    """定位项目的源视频; 找不到返回 None (调用方决定报什么错)"""
    src = resolve_source(project, pdir)
    if src:
        return src
    # 下载来源; 兼容旧项目 (本地导入曾复制进 media/) 或源文件被移走的项目
    media = Path(pdir) / "media"
    for pat in ("video.mp4", "video.mkv", "video.webm", "video.*"):
        hits = sorted(media.glob(pat))
        if hits:
            return hits[0]
    return None
