"""
步骤 1: download —— 视频获取
============================
两种来源:
  youtube (source_url 填链接): yt-dlp 下载到 media/video.<ext>
      - 幂等由项目产物与输入指纹保证；不使用只记录 ID、不校验文件的全局归档
      - cookies / 代理 / 分辨率 均从 config.download 读取
  local (source_url 填本地路径): 两种形态 (见 video_source.py) ——
      外部引用 (绝对路径): 原文件就地引用零拷贝, 产物登记指向原文件;
      拖入导入 (相对路径): 副本在项目目录 media/ 内, 是项目自有文件。
      原文件/副本被移动删除后重跑会报 SOURCE_NOT_FOUND。

产物: 下载 → media/video.<ext>; 本地 → 原文件本身 (均登记为 download/video.mp4 逻辑名)
"""
import json
import math
import re
import tempfile
import threading
from pathlib import Path

from dubforge.video_info import SOURCE_META_FIELDS, _build_video_info, _read_json
from dubforge.errors import DubForgeError
from dubforge.progress import RateProgress, _bind
from dubforge.pipeline.video_source import find_final_video, resolve_source
from dubforge.utils.fs import atomic_replace
from dubforge.utils.srt import SubLine, write_srt


# 下载完成后让 yt-dlp 直接回传来源元数据；用 JSON 标量可安全保留多行描述
_SOURCE_META_FIELDS = SOURCE_META_FIELDS


def _ydl_format(resolution: int) -> str:
    # 优先该分辨率及以下的最佳 bv+ba; 兜底任意最佳
    return (f"bestvideo[height<={resolution}]+bestaudio/"
            f"best[height<={resolution}]/best")


def _ytdlp_cmd(ctx, url: str, out_template: str) -> list[str]:
    """yt-dlp 子进程命令行 (cookies/代理/格式只来自本次配置快照)。
    子进程化是为了可即时终止 —— 下载是最长的步骤, 取消必须马上停。
    不读取系统 yt-dlp 配置或下载归档，避免隐式跳过、改输出目录或忽略错误。"""
    import sys
    dcfg = ctx.cfg("download") or {}
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config",
        # YouTube n-challenge 需要 JS 运行时 + 挑战求解脚本 (EJS); deno 已是前置,
        # 求解脚本按官方推荐从 GitHub 拉取。缺它时部分格式缺失, 下载易被限流掐断。
        "--remote-components", "ejs:github",
        "-f", _ydl_format(int(dcfg.get("resolution", 1080))),
        "-o", out_template,
        "--merge-output-format", "mp4",
        "--no-playlist", "--no-write-comments", "--newline", "--progress", "--no-colors",
        "--socket-timeout", "30", "--retries", "5", "--fragment-retries", "5",
        "--retry-sleep", "http:exp=1:10", "--retry-sleep", "fragment:exp=1:10",
        "--continue", "--part", "--abort-on-unavailable-fragments",
        "--print", "before_dl:DUBFORGE_PLAN:%(requested_formats.:.{format_id,filesize,filesize_approx},.{format_id,filesize,filesize_approx})j",
        "--progress-template", "download:DUBFORGE_BYTES:%(info.format_id)j\t%(progress.{status,downloaded_bytes,total_bytes,total_bytes_estimate})j",
        "--print", "after_move:%(filepath)s",
        "--print", "after_move:%(title)s",
        *[print_arg for field in _SOURCE_META_FIELDS
          for print_arg in ("--print",
                            f"after_move:DUBFORGE_META_{field.upper()}:%({field})j")],
    ]
    cookies = dcfg.get("cookies_path")
    if cookies and Path(cookies).exists():
        cmd += ["--cookies", str(cookies)]
    proxy = dcfg.get("proxy")
    if proxy:
        cmd += ["--proxy", str(proxy)]
    cmd += ["--", url]
    return cmd


_VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts", ".m4v"}


class DownloadError(DubForgeError):
    code = "DOWNLOAD_FAIL"


def _diagnostic_text(text: str) -> str:
    """保留错误而非进度数据；媒体直链可能包含签名，日志不暴露其参数或凭据。"""
    text = "\n".join(line for line in text.splitlines() if not line.startswith("DUBFORGE_"))
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[已隐藏凭据]@", text)
    return re.sub(r"(https?://[^\s?#]+)[?#][^\s]+", r"\1?[已隐藏参数]", text)[-4000:]


def _download_error(detail: str) -> DownloadError:
    """错误码保持稳定；用真实原因给出可操作的中文提示。"""
    cause = detail.lower()
    if "timed out" in cause or "timeout" in cause:
        message = "视频下载超时，自动重试后仍未完成"
        suggestion = "请检查网络或代理连接后重试；这不是整步运行时限，单纯调大步骤超时通常无效。"
    elif "429" in cause or "too many requests" in cause:
        message = "视频平台请求过于频繁，下载被限流"
        suggestion = "请稍后重试，避免连续重复下载；检查代理是否有其他高频请求。"
    elif any(marker in cause for marker in ("sign in", "login required", "confirm you're not a bot", "private video")):
        message = "视频平台要求登录或验证，无法下载"
        suggestion = "请先在浏览器确认视频可访问；必要时在获取视频设置中配置有效的 Cookies 文件。"
    elif "403" in cause or "forbidden" in cause:
        message = "视频平台拒绝下载请求（403）"
        suggestion = "请检查视频访问权限、网络或代理后重试；若持续失败，请检查 Cookies 和下载组件适配。"
    elif any(marker in cause for marker in ("proxyerror", "unable to connect to proxy", "connection refused", "getaddrinfo failed")):
        message = "无法连接视频平台或代理"
        suggestion = "请检查网络、代理服务及获取视频中的代理地址后重试。"
    elif any(marker in cause for marker in ("ssl", "eof occurred", "connection reset", "connection aborted")):
        message = "下载连接被中断，自动重试后仍未完成"
        suggestion = "通常是代理节点不稳定或平台掐断了连接；请更换代理节点后重试，若反复出现可稍后再试。"
    elif "requested format is not available" in cause:
        message = "视频平台未提供可用的下载格式"
        suggestion = "请确认视频可播放并检查分辨率设置；若仍失败，请检查下载组件适配。"
    elif any(marker in cause for marker in ("no space left", "not enough space", "disk full")):
        message = "磁盘空间不足，无法完成视频下载"
        suggestion = "重新下载期间需要同时存放旧视频和临时文件；请释放项目所在磁盘空间后重试。"
    else:
        message = "yt-dlp 下载失败"
        suggestion = "具体原因已记录在步骤日志中的「下载错误详情」，请据此检查网络、访问权限或下载组件。"
    return DownloadError(message, suggestion=suggestion, detail=_diagnostic_text(detail))


def _run_ytdlp(ctx, url: str, out_template: str) -> dict:
    """子进程方式调 yt-dlp; 取消即终止进程树; 返回 {title, filepath, source_meta}"""
    cmd = _ytdlp_cmd(ctx, url, out_template)
    ctx.log("下载连接读取超时 30 秒；HTTP 与分片错误最多各重试 5 次，间隔 1–10 秒")
    last_pct = -1
    totals, received = {}, {}
    tracker = None
    plan_ready = False
    progress_lock = threading.Lock()
    source_meta: dict = {}

    def on_meta(line: str) -> None:
        name, _, payload = line.partition(":")
        field = name.removeprefix("DUBFORGE_META_").lower()
        if not field:
            return
        try:
            value = json.loads(payload)
            source_meta[field] = value
        except (ValueError, TypeError):
            source_meta[field] = str(payload)

    def on_stdout(line: str) -> None:
        nonlocal plan_ready
        if line.startswith("DUBFORGE_META_"):
            on_meta(line)
            return
        if line.startswith("DUBFORGE_BYTES:"):
            on_stderr(line)
            return
        if not line.startswith("DUBFORGE_PLAN:"):
            return
        try:
            plan = json.loads(line.partition(":")[2])
            with progress_lock:
                for item in plan if isinstance(plan, list) else [plan]:
                    totals[str(item["format_id"])] = float(item.get("filesize") or item.get("filesize_approx") or 0)
                plan_ready = True
        except (ValueError, TypeError, KeyError):
            pass

    def on_stderr(line: str) -> None:
        nonlocal last_pct, tracker
        if line.startswith(("DUBFORGE_PLAN:", "DUBFORGE_META_")):
            on_stdout(line)
            return
        if not line.startswith("DUBFORGE_BYTES:"):
            # 超时重试不能只停住百分比：实时保留下载器的告警和重试原因。
            if line.startswith(("WARNING:", "ERROR:", "[download] Got error", "[download] Sleeping")):
                ctx.log(f"下载器: {_diagnostic_text(line)}")
            return
        with progress_lock:
            try:
                if not plan_ready:
                    return
                format_json, payload = line.partition(":")[2].split("\t", 1)
                key, data = str(json.loads(format_json)), json.loads(payload)
                first = key not in received
                received[key] = max(received.get(key, 0), float(data.get("downloaded_bytes") or 0))
                totals[key] = max(received[key], float(data.get("total_bytes") or data.get("total_bytes_estimate") or totals.get(key, 0)))
                if not totals or any(value <= 0 for value in totals.values()):
                    return
                total, done = sum(totals.values()), sum(received.values())
                if tracker is None:
                    first = True
                    tracker = RateProgress(ctx, total, end=100, scope="phase")
                tracker.total = total
                tracker.update(done, cached=first)
                pct = done / total * 100
                if int(pct) // 5 != last_pct:
                    ctx.log(f"下载中 {pct:.1f}%（音视频合计，按本次累计平均速度估算剩余时间）")
                    last_pct = int(pct) // 5
            except (ValueError, TypeError, KeyError):
                return

    try:
        rc, out, err = ctx.run_subprocess(cmd, on_stdout=on_stdout, on_stderr=on_stderr)
    finally:
        _bind(ctx, None)
    if rc != 0:
        error = _download_error(err or out or f"下载进程退出码 {rc}")
        ctx.log(f"下载错误详情:\n{error.detail}")
        raise error
    # 过滤结构化进度后，--print 留下文件路径和标题。
    lines = [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.startswith("DUBFORGE_")]
    filepath = next((ln for ln in lines
                     if Path(ln).suffix.lower() in _VIDEO_EXT), "")
    title = next((ln for ln in lines if ln != filepath), "")
    if not filepath:
        ctx.log("yt-dlp 未报告输出文件，将检查本次临时目录")
    return {"title": title, "filepath": filepath, "source_meta": source_meta}


def _find_downloaded(media_dir: Path) -> Path | None:
    """在 media/ 里找视频文件 (yt-dlp 输出名可能带后缀变化; 过程文件不算)"""
    return find_final_video(media_dir)


def _validate_download(ctx, path: Path) -> None:
    """转正前探测容器、视频流和时长；不是耗时的全片解码校验。"""
    if not path.is_file() or path.stat().st_size == 0:
        raise DownloadError("下载结果为空，未替换旧视频", code="DOWNLOAD_INVALID")
    ctx.check_cancel()
    try:
        rc, out, err = ctx.run_subprocess([
            "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
            "-of", "json", str(path)])
    except OSError as exc:
        raise DownloadError("无法启动视频校验工具，未替换旧视频", code="DOWNLOAD_VALIDATION_UNAVAILABLE",
                            suggestion="请在环境检查中确认 ffprobe 可用后重试。") from exc
    try:
        info = json.loads(out)
        duration = float(info.get("format", {}).get("duration", 0))
        valid = (rc == 0 and math.isfinite(duration) and duration > 0
                 and any(s.get("codec_type") == "video" for s in info.get("streams", [])))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        detail = _diagnostic_text(err or out)
        ctx.log(f"下载文件校验失败:\n{detail}")
        raise DownloadError("下载结果无法确认为完整视频，未替换旧视频", code="DOWNLOAD_INVALID",
                            suggestion="临时文件未转正，请检查下载日志后重试。", detail=detail)


def _download_video(ctx, url: str, media: Path) -> tuple[Path, dict]:
    """本次下载隔离于正式文件；成功并校验后在同一磁盘原子替换。"""
    old = _find_downloaded(media)
    if old:
        ctx.log(f"重新获取视频：保留 {old.name}，新文件下载并校验成功后再替换")
    with tempfile.TemporaryDirectory(dir=media, prefix=".download-", suffix=".tmp",
                                     ignore_cleanup_errors=True) as directory:
        staging = Path(directory).resolve()
        try:
            info = _run_ytdlp(ctx, url, str(staging / "video.%(ext)s"))
            ctx.check_cancel()
            candidate = Path(info["filepath"]).resolve() if info.get("filepath") else _find_downloaded(staging)
            # 仅接收本次目录中的精确最终名，不能回退正式目录，把旧文件误报为本次成功。
            if (not candidate or candidate.parent != staging
                    or candidate.suffix.lower() not in _VIDEO_EXT
                    or candidate.stem != "video" or not candidate.is_file()):
                raise DownloadError("下载结束但未找到本次完整视频，未替换旧文件", code="DOWNLOAD_UNKNOWN",
                                    suggestion="请查看步骤日志后重试；已有旧视频不会被当成本次下载结果。")
            _validate_download(ctx, candidate)
            ctx.check_cancel()
            dst = media / candidate.name
            tmp = candidate.with_suffix(candidate.suffix + ".tmp")
            try:
                atomic_replace(candidate, tmp)
                atomic_replace(tmp, dst)
            except OSError as exc:
                raise DownloadError("新视频已下载，但无法替换正式文件", code="DOWNLOAD_REPLACE_FAILED",
                                    suggestion="旧视频仍保留；请检查文件是否被其他程序占用，以及磁盘空间和写入权限。") from exc
            return dst, info
        except BaseException:
            ctx.log("本次下载未转正；已有源视频保持不变，清理本次下载临时文件")
            raise


def _write_meta(path: Path, meta: dict) -> None:
    """元数据也遵守临时文件→原子替换，避免中断留下半个 JSON。"""
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        atomic_replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)




def _ensure_video_info(ctx, kind: str, title: str, source_url: str,
                       raw_meta: dict | None = None, *, refresh: bool) -> dict:
    """读取/补生成 video_info.json，并保证当前一次产物登记存在。"""
    path = ctx.artifact_dir() / "video_info.json"
    current = None if refresh else _read_json(path)
    if current is None:
        current = _build_video_info(kind, title, source_url, ctx, raw_meta)
        ctx.write_json("video_info.json", current)
        if current.get("summary_status") == "generated":
            ctx.log("✅ 已整理视频信息（中文标题与内容概要）")
        elif current.get("summary_status") == "local_no_metadata":
            ctx.log("已生成本地视频基础信息；转录后可再补充内容概要")
    else:
        # 旧工程升级到新版本时，同名产物可能还在但尚未登记到当前运行记录。
        ctx.write_json("video_info.json", current)
    return current


def run(ctx) -> None:
    media = ctx.pdir / "media"
    media.mkdir(exist_ok=True)
    kind = ctx.project.source_kind
    if kind not in ("local", "youtube", "link"):
        raise DubForgeError(f"未知来源类型: {kind}", code="SOURCE_KIND_UNKNOWN")

    # 幂等: 输入未变化且文件在时复用；重建只绕过复用，绝不提前删除旧视频。
    # (本地导入不受影响, 用户的原文件任何情况下都不删)
    existing = None
    rebuild = bool(getattr(ctx, "rebuild", False) or getattr(ctx, "force", False))
    if kind == "local":
        # 明确填写的本地源永远以 source_url 为准，不能因 media/ 恰好有旧文件
        # 就静默复用错误视频；只有早期没有 source_url 的项目才回退 media/ 副本。
        existing = _find_downloaded(media) if not ctx.project.source_url else None
    else:
        if not rebuild:
            existing = _find_downloaded(media)

    if existing:
        ctx.log(f"已存在视频: {existing.name}, 跳过获取")
        ctx.register_file("video.mp4", existing, "video")
        meta_path = media / "meta.json"
        repair_meta = not meta_path.is_file()
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {
                "title": ctx.project.title or existing.stem,
                "file": existing.name,
                "size": existing.stat().st_size,
            }
        except (OSError, ValueError, TypeError):
            repair_meta = True
            meta = {"title": ctx.project.title or existing.stem,
                    "file": existing.name, "size": existing.stat().st_size}
        if not isinstance(meta, dict):
            repair_meta = True
            meta = {"title": ctx.project.title or existing.stem,
                    "file": existing.name, "size": existing.stat().st_size}
        if repair_meta:
            _write_meta(meta_path, meta)
        ctx.write_json("meta.json", meta)
        _ensure_video_info(
            ctx, kind, str(meta.get("title") or ctx.project.title or existing.stem),
            ctx.project.source_url, refresh=False)
        return

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
        _ensure_video_info(ctx, kind, title, str(ctx.project.source_url),
                           refresh=True)
    elif kind in ("youtube", "link"):   # yt-dlp 支持的任意视频站点 (YouTube/B站/Twitter...)
        url = ctx.project.source_url
        if not url:
            raise DubForgeError("项目没有填写视频链接", code="SOURCE_URL_EMPTY")
        ctx.log(f"开始下载: {_diagnostic_text(url)}")
        dst, info = _download_video(ctx, url, media)
        title = info.get("title") or "未命名视频"
        raw_meta = info.get("source_meta") if isinstance(info.get("source_meta"), dict) else {}
        ctx.log(f"标题: {title}")
        ctx.check_cancel()
        _ensure_video_info(ctx, kind, title, url, raw_meta, refresh=True)
    else:
        raise DubForgeError(f"未知来源类型: {kind}", code="SOURCE_KIND_UNKNOWN")

    ctx.report_progress(90)
    # 简单元数据产物 (标题供项目列表展示; duration 供后续步骤参考)
    meta = {"title": title, "file": dst.name, "size": dst.stat().st_size}
    meta_path = media / "meta.json"
    _write_meta(meta_path, meta)
    ctx.write_json("meta.json", meta)
    ctx.register_file("video.mp4", dst, "video", replace_existing=True)
    if kind != "local":
        # 新文件与索引已就绪才移除旧容器格式，避免解析器优先选到旧 video.mp4。
        for ext in _VIDEO_EXT:
            old = media / ("video" + ext)
            if old != dst and old.is_file():
                try:
                    old.unlink()
                except OSError:
                    ctx.log(f"旧格式文件 {old.name} 暂未清理；当前视频以 meta.json 登记为准")
    ctx.log(f"✅ 视频就绪: {dst.name} ({dst.stat().st_size // 1048576} MB)")


# 供 tests / API 侧构造假 srt 用的公共出口 (M2b 文本链使用)
__all__ = ["DubForgeError", "SubLine", "re", "run", "write_srt"]
