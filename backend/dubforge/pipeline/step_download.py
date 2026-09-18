# -*- coding: utf-8 -*-
"""
步骤 1: download —— 视频获取
============================
两种来源:
  youtube (source_url 填链接): yt-dlp 下载到 media/video.<ext>
      - 全局去重: data/download_archive.txt 记录已下载 ID, 重复链接直接复用已有项目产物
      - cookies / 代理 / 分辨率 均从 config.download 读取
  local (source_url 填本地路径): 两种形态 (见 video_source.py) ——
      外部引用 (绝对路径): 原文件就地引用零拷贝, 产物登记指向原文件;
      拖入导入 (相对路径): 副本在项目目录 media/ 内, 是项目自有文件。
      原文件/副本被移动删除后重跑会报 SOURCE_NOT_FOUND。

产物: 下载 → media/video.<ext>; 本地 → 原文件本身 (均登记为 download/video.mp4 逻辑名)
"""
import json
import re
from pathlib import Path

from dubforge import config
from dubforge.errors import DubForgeError
from dubforge.pipeline.video_source import find_final_video, resolve_source
from dubforge.utils.srt import SubLine, write_srt


def _ydl_format(resolution: int) -> str:
    # 优先该分辨率及以下的最佳 bv+ba; 兜底任意最佳
    return (f"bestvideo[height<={resolution}]+bestaudio/"
            f"best[height<={resolution}]/best")


def _ytdlp_cmd(ctx, url: str, out_template: str) -> list[str]:
    """yt-dlp 子进程命令行 (与库方式同参: 归档/cookies/代理/格式)。
    子进程化是为了可即时终止 —— 下载是最长的步骤, 取消必须马上停。"""
    import sys
    dcfg = ctx.cfg("download") or {}
    archive = config.data_root() / "download_archive.txt"
    archive.touch(exist_ok=True)
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", _ydl_format(int(dcfg.get("resolution", 1080))),
        "-o", out_template,
        "--merge-output-format", "mp4",
        "--no-playlist", "--newline", "--progress", "--retries", "3",
        "--download-archive", str(archive),   # 全局去重: 同 ID 不重下
        "--print", "after_move:%(filepath)s",
        "--print", "after_move:%(title)s",
        url,
    ]
    cookies = dcfg.get("cookies_path")
    if cookies and Path(cookies).exists():
        cmd += ["--cookies", str(cookies)]
    proxy = dcfg.get("proxy")
    if proxy:
        cmd += ["--proxy", str(proxy)]
    return cmd


_VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m4v"}


def _run_ytdlp(ctx, url: str, out_template: str) -> dict:
    """子进程方式调 yt-dlp; 取消即终止进程树; 返回 {title, filepath}"""
    cmd = _ytdlp_cmd(ctx, url, out_template)
    ctx.log(f"yt-dlp: {url}")
    last_pct = -1

    def on_stderr(line: str) -> None:
        # [download]  12.3% of  ~45.20MiB at  1.23MiB/s ETA 00:30
        nonlocal last_pct
        m = re.match(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%", line)
        if m:
            pct = float(m.group(1))
            if int(pct) != last_pct and int(pct) % 5 == 0:
                ctx.log(f"下载中 {pct:.1f}%")
                last_pct = int(pct)
            ctx.report_progress(min(89.0, pct * 0.89))

    rc, out, err = ctx.run_subprocess(cmd, on_stderr=on_stderr)
    if rc != 0:
        raise DubForgeError("yt-dlp 下载失败", code="DOWNLOAD_FAIL",
                            detail=(err or out)[-1500:])
    # stdout 只有 --print 的两行: 文件路径 + 标题 (顺序与参数一致)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    filepath = next((ln for ln in lines
                     if Path(ln).suffix.lower() in _VIDEO_EXT), "")
    title = next((ln for ln in lines if ln != filepath), "")
    if not filepath:
        # 归档命中 (此前下过且文件已删) 时 yt-dlp 不产出 → 让调用方走兜底检查
        ctx.log("yt-dlp 未报告输出文件 (可能命中全局下载归档)")
    return {"title": title, "filepath": filepath}


def _find_downloaded(media_dir: Path) -> Path | None:
    """在 media/ 里找视频文件 (yt-dlp 输出名可能带后缀变化; 过程文件不算)"""
    return find_final_video(media_dir)


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
        src = resolve_source(ctx.project, ctx.pdir)
        if not src:
            raise DubForgeError(
                f"本地视频不存在: {ctx.project.source_url or '(未提供)'}",
                code="SOURCE_NOT_FOUND",
                suggestion="原文件可能被移动或删除; 请确认路径, 或重建项目重新选择文件。")
        ctx.log(f"引用本地视频 (不复制): {src}")
        ctx.report_progress(10)
        title = src.stem
        dst = src
    elif kind in ("youtube", "link"):   # yt-dlp 支持的任意视频站点 (YouTube/B站/Twitter...)
        url = ctx.project.source_url
        if not url:
            raise DubForgeError("项目没有填写视频链接", code="SOURCE_URL_EMPTY")
        ctx.log(f"开始下载: {url}")
        info = _run_ytdlp(ctx, url, str(media / "video.%(ext)s"))
        title = info["title"] or "未命名视频"
        ctx.log(f"标题: {title}")
        ctx.check_cancel()
        dst = (Path(info["filepath"]) if info["filepath"] else None)
        if not dst or not dst.exists():
            dst = _find_downloaded(media)     # 归档命中/改名兜底: 扫 media/
        if not dst:
            raise DubForgeError("yt-dlp 报告完成但未找到视频文件", code="DOWNLOAD_UNKNOWN")
    else:
        raise DubForgeError(f"未知来源类型: {kind}", code="SOURCE_KIND_UNKNOWN")

    ctx.report_progress(90)
    # 简单元数据产物 (标题供项目列表展示; duration 供后续步骤参考)
    meta = {"title": title, "file": dst.name, "size": dst.stat().st_size}
    (media / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
    ctx.write_json("meta.json", meta)
    ctx.register_file("video.mp4", dst, "video")
    ctx.log(f"✅ 视频就绪: {dst.name} ({dst.stat().st_size // 1048576} MB)")


# 供 tests / API 侧构造假 srt 用的公共出口 (M2b 文本链使用)
__all__ = ["run", "SubLine", "write_srt", "DubForgeError", "re"]
