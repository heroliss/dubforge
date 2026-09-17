# -*- coding: utf-8 -*-
"""
步骤 1: download —— 视频获取
============================
两种来源:
  youtube (source_url 填链接): yt-dlp 下载到 media/video.<ext>
      - 全局去重: data/download_archive.txt 记录已下载 ID, 重复链接直接复用已有项目产物
      - cookies / 代理 / 分辨率 均从 config.download 读取
  local (source_url 填本地路径): 校验存在后复制进 media/ (复制而非引用: 项目目录自包含,
      删源文件不影响流水线; 超大文件场景后续可加 link 模式)

产物: media/video.<ext> (登记为 download/video.mp4 逻辑名)
"""
import json
import re
import shutil
from pathlib import Path

from dubforge import config
from dubforge.errors import DubForgeError
from dubforge.utils.srt import SubLine, write_srt


def _ydl_format(resolution: int) -> str:
    # 优先该分辨率及以下的最佳 bv+ba; 兜底任意最佳
    return (f"bestvideo[height<={resolution}]+bestaudio/"
            f"best[height<={resolution}]/best")


def _run_ytdlp(ctx, url: str, out_template: str) -> dict:
    """库方式调 yt-dlp; 返回 info_dict 关键字段"""
    import yt_dlp
    dcfg = ctx.cfg("download") or {}
    archive = config.data_root() / "download_archive.txt"
    archive.touch(exist_ok=True)

    opts = {
        "format": _ydl_format(int(dcfg.get("resolution", 1080))),
        "outtmpl": out_template,
        "download_archive": str(archive),      # 全局去重: 同 ID 不重下
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "progress_hooks": [_progress_hook(ctx)],
        # Windows 下断点续传的文件片段
        "continuedl": True,
    }
    cookies = dcfg.get("cookies_path")
    if cookies and Path(cookies).exists():
        opts["cookiefile"] = cookies
    proxy = dcfg.get("proxy")
    if proxy:
        opts["proxy"] = proxy

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # extract_info 对已归档视频也返回 info (download=None), 正好用于幂等
        return {
            "id": info.get("id"),
            "title": info.get("title") or "",
            "duration": info.get("duration"),
            "filepath": ydl.prepare_filename(info),
        }


def _progress_hook(ctx):
    def hook(d):
        if d.get("status") == "downloading":
            pct = d.get("_percent_str", "").strip()
            speed = d.get("_speed_str", "").strip()
            ctx.log(f"下载中 {pct} {speed}")
        elif d.get("status") == "finished":
            ctx.log(f"下载完成: {d.get('filename')}")
    return hook


def _find_downloaded(media_dir: Path) -> Path | None:
    """在 media/ 里找视频文件 (yt-dlp 输出名可能带后缀变化)"""
    for pat in ("video.mp4", "video.mkv", "video.webm", "video.*"):
        hits = sorted(media_dir.glob(pat))
        if hits:
            return hits[0]
    return None


def run(ctx) -> None:
    media = ctx.pdir / "media"
    media.mkdir(exist_ok=True)

    # 幂等: 已有产物且文件在 → 跳过获取。
    # 注意 force 语义: 执行器会先删"已登记产物行", 但获取本身没有过期概念
    # (本地文件还在/链接已在归档), 文件只要在 media/ 就直接复用。
    existing = _find_downloaded(media)
    if existing:
        ctx.log(f"已存在视频: {existing.name}, 跳过获取")
        ctx.register_file("video.mp4", existing, "video")
        meta_path = media / "meta.json"
        if meta_path.exists():
            try:
                ctx.write_json("meta.json",
                               json.loads(meta_path.read_text(encoding="utf-8")))
            except Exception:
                pass
        return

    kind = ctx.project.source_kind
    if kind == "local":
        src = Path(ctx.project.source_url or "")
        if not src.exists():
            raise DubForgeError(f"本地视频不存在: {src}", code="SOURCE_NOT_FOUND",
                                suggestion="请确认路径正确, 或重新创建项目并选择有效文件。")
        dst = media / f"video{src.suffix.lower()}"
        ctx.log(f"导入本地视频: {src}")
        shutil.copy2(src, dst)
        title = src.stem
    elif kind == "youtube":
        url = ctx.project.source_url
        if not url:
            raise DubForgeError("项目没有填写视频链接", code="SOURCE_URL_EMPTY")
        ctx.log(f"开始下载: {url}")
        info = _run_ytdlp(ctx, url, str(media / "video.%(ext)s"))
        title = info["title"]
        ctx.log(f"标题: {title}")
        dst = _find_downloaded(media)
        if not dst:
            raise DubForgeError("yt-dlp 报告完成但未找到视频文件", code="DOWNLOAD_UNKNOWN",
                                detail=json.dumps(info, ensure_ascii=False)[:2000])
    else:
        raise DubForgeError(f"未知来源类型: {kind}", code="SOURCE_KIND_UNKNOWN")

    # 简单元数据产物 (标题供项目列表展示; duration 供后续步骤参考)
    meta = {"title": title, "file": dst.name, "size": dst.stat().st_size}
    (media / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
    ctx.write_json("meta.json", meta)
    ctx.register_file("video.mp4", dst, "video")
    ctx.log(f"✅ 视频就绪: {dst.name} ({dst.stat().st_size // 1048576} MB)")


# 供 tests / API 侧构造假 srt 用的公共出口 (M2b 文本链使用)
__all__ = ["run", "SubLine", "write_srt", "DubForgeError", "re"]
