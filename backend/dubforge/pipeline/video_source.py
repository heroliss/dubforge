"""源视频定位 (全管线单一出口)
==============================
本地导入有两种形态, source_url 双制式:
  外部引用 (浏览选择/粘贴路径): 绝对路径, 原文件就地引用零拷贝, 项目目录只登记不落副本。
  拖入导入: 浏览器拿不到拖入文件的真实磁盘路径, 只能传内容 —— 副本存进项目目录 media/,
  source_url 记项目内相对路径, 是项目自有文件, 随项目存亡。
下载来源: media/video.<ext>。
下游 (preprocess/assemble/workbench) 一律从这里解析, 不各自 glob。
"""
import json
import re
from pathlib import Path

# yt-dlp 过程文件: .part (下载中) / .ytdl; 合并前的单流中间产物 video.f137.mp4 —— 只有画面没有音轨
_PARTIAL = re.compile(r"\.part$|\.ytdl$|\.tmp$|\.f\d+\.", re.IGNORECASE)
_VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m4v"}


def find_final_video(media_dir: Path) -> Path | None:
    """media/ 里找"最终"视频文件: 精确名优先; 过程文件一律不算 (否则下载中途会把
    无声的单流中间产物当成源视频, 预览就没声音了)"""
    # 容器格式变更后，旧文件可能因被占用而暂未清理；优先使用已转正的登记文件。
    try:
        name = json.loads((media_dir / "meta.json").read_text(encoding="utf-8")).get("file", "")
        preferred = media_dir / name if isinstance(name, str) else None
        if (preferred and preferred.parent == media_dir and preferred.stem == "video"
                and preferred.suffix.lower() in _VIDEO_EXT
                and preferred.is_file() and _nonempty_file(preferred)):
            return preferred
    except (OSError, ValueError, AttributeError):
        pass
    for pat in ("video.mp4", "video.mkv", "video.webm", "video.*"):
        hits = [h for h in sorted(media_dir.glob(pat)) if not _PARTIAL.search(h.name)]
        hits = [h for h in hits
                if h.suffix.lower() in _VIDEO_EXT and h.is_file() and _nonempty_file(h)]
        if hits:
            return hits[0]
    return None


def _nonempty_file(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def resolve_source(project, pdir: Path) -> Path | None:
    """把 source_url 解析为绝对路径 (相对路径 = 项目目录内副本); 文件不存在返回 None"""
    if project.source_kind != "local" or not project.source_url:
        return None
    p = Path(project.source_url)
    if not p.is_absolute():
        root = Path(pdir).resolve()
        p = (root / p).resolve()
        if not p.is_relative_to(root):
            return None
    return p if p.is_file() else None


def resolve_video(project, pdir: Path) -> Path | None:
    """定位项目的源视频; 找不到返回 None (调用方决定报什么错)"""
    src = resolve_source(project, pdir)
    if src:
        return src
    # 下载来源; 兼容旧项目 (早期本地导入没有 source_url) 的 media/ 副本。
    # 已明确填写本地源路径却找不到时不能回退旧副本，否则会把旧视频误当新源。
    if project.source_kind == "local" and project.source_url:
        return None
    return find_final_video(Path(pdir) / "media")
