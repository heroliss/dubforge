"""
步骤 9: assemble —— 成片合成
============================
两阶段:
  A) 铺轨: 批量配音产生的句级音频按时间轴铺到与视频等长的完整人声轨 (audio/dub_full.wav),
     峰值归一 + 句间淡入淡出; loudnorm 开启时再做 EBU R128 单遍归一
  B) 封装: ffmpeg 混音 —— 背景音量 + 人声 ducking (sidechaincompress,
     说话时自动压低背景) + 可选字幕烧录; 视频流直拷不重编码

产物: 每种组合一个当前成品，保留首次命名，重跑成功后原子替换。
外挂字幕由字幕导出步骤单独提供。
"""
import datetime
import math
import subprocess
from pathlib import Path

from dubforge.errors import DubForgeError
from dubforge.pipeline.assembly_outputs import OUTPUT_VARIANTS, VARIANT_LABELS, default_variants, validate_variants, dimensions, current_output
from dubforge.progress import RateProgress
from dubforge.utils import audio as au
from dubforge.utils.fs import atomic_replace
from dubforge.pipeline.comment_overlay import prepare_filter


def _ffprobe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", check=False)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _task_range(ctx, total: float) -> tuple[float, float]:
    """读取并校验试合成区域；区域使用视频原始时间轴。"""
    data = ctx.task_data if ctx.task_data.get("scope") == "preview" else {}
    try:
        start, end = float(data.get("start")), float(data.get("end"))
    except (TypeError, ValueError) as exc:
        raise DubForgeError("试合成区域无效，请重新选择起止时间", code="ASSEMBLE_PREVIEW_RANGE") from exc
    if not all(math.isfinite(value) for value in (start, end)):
        raise DubForgeError("试合成区域无效，请填写有限时间", code="ASSEMBLE_PREVIEW_RANGE")
    if start < 0 or end <= start or end > total + 0.05:
        raise DubForgeError(
            f"试合成区域需满足 0 ≤ 起点 < 终点 ≤ {total:.2f}s",
            code="ASSEMBLE_PREVIEW_RANGE", suggestion="请在视频预览中重新拖选一段区域。")
    if end - start < 0.2:
        raise DubForgeError("试合成区域太短，至少需要 0.2 秒", code="ASSEMBLE_PREVIEW_RANGE")
    return round(start, 3), round(min(end, total), 3)


def _require_dubbing(ctx):
    from dubforge.pipeline.dependencies import Dependencies
    deps = Dependencies(ctx.project)
    # 使用任务冻结的配置；不让运行中的全局配置改变本次校验语义。
    deps.cfg = ctx.cfg
    return deps.require_dubbing()


def _mix_timeline(ctx, pdir: Path, total: float, *,
                  start: float | None = None, end: float | None = None, output_name=None, register=True) -> Path:
    """阶段 A: 按时间轴铺轨 + 归一；试合成时只为选定区域生成独立音频。"""
    from dubforge.pipeline.step_dubtask import build_task
    from dubforge.pipeline.dubbing_edit import production_lines, take_name
    task = build_task(ctx)
    _require_dubbing(ctx)
    lines = production_lines(ctx.project, task['lines'], pdir)
    preview = start is not None and end is not None
    region_start = float(start or 0.0)
    region_end = float(end if end is not None else total)
    entries = []
    original = None
    for line in lines:
        ctx.check_cancel()
        if line['source'] == 'original':
            clip_start = max(float(line['start']), region_start)
            clip_end = min(float(line['end']), region_end)
            if clip_end <= clip_start:
                continue
            if original is None:
                original = au.load_mono_32k(ctx.upstream('preprocess', 'vocal.wav'))
            sample_start = round(clip_start * au.SR)
            sample_end = round(clip_end * au.SR)
            entries.append((clip_start - region_start,
                            original[sample_start:sample_end]))
        elif line['take'] is not None:
            wav = ctx.upstream('tts', take_name(line['id'], line['take']))
            audio = au.load_mono_32k(wav)
            audio_start = float(line['start'])
            audio_end = audio_start + len(audio) / au.SR
            clip_start = max(audio_start, region_start)
            clip_end = min(audio_end, region_end)
            if clip_end <= clip_start:
                continue
            sample_start = round((clip_start - audio_start) * au.SR)
            sample_end = round((clip_end - audio_start) * au.SR)
            entries.append((clip_start - region_start, audio[sample_start:sample_end]))

    duration = max(0.0, region_end - region_start)
    # 全部选择空位也有明确语义：保留视频与背景，人声轨为静音。
    ctx.log(f"铺轨: 区域内 {len(entries)} 句 → {duration:.1f}s 人声轨"
            if preview else f"铺轨: {len(entries)} 句 → {total:.1f}s 人声轨")
    # 句首尾 10ms 淡入淡出, 消除逐句拼接处的咔哒声 (au.fade_in_out)
    entries = [(offset, au.fade_in_out(audio)) for offset, audio in entries]
    mixed = au.mix_over_timeline(entries, duration)
    mixed = au.peak_normalize(mixed, -3.0)

    stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    out = (pdir / "audio" / f"preview_dub_{stamp}.wav" if preview
           else pdir / "audio" / "dub_full.wav")
    if output_name:
        out = pdir / 'audio' / output_name
    out.parent.mkdir(exist_ok=True)
    import soundfile as sf
    tmp = out.with_suffix(".wav.tmp")
    # .tmp 后缀无法推断格式, 显式指定 WAV (与 ffmpeg 原子写同理)
    try:
        sf.write(str(tmp), mixed, au.SR, subtype="PCM_16", format="WAV")
        atomic_replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)

    # EBU R128 响度归一 (单遍, 足够配音场景)
    if ctx.cfg("assemble.loudnorm", True):
        norm = out.with_suffix(".tmp_norm.wav")
        try:
            rc, _, err = ctx.run_subprocess(
                ["ffmpeg", "-y", "-i", str(out),
                 "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                 "-ar", "32000", "-ac", "1", "-f", "wav", str(norm)])
            if rc != 0:
                raise DubForgeError("响度归一失败 (ffmpeg)", code="FFMPEG_FAIL",
                                    detail=err[-1500:])
            atomic_replace(norm, out)
        finally:
            norm.unlink(missing_ok=True)
        ctx.log("响度归一 (EBU R128, I=-16) 完成")
    if register:
        ctx.register_file(out.name, out, "audio")
    return out


def _write_preview_srt(source: Path, start: float, end: float, target: Path) -> None:
    """把原时间轴 SRT 裁剪并平移到试合成片段，保留多行字幕文本。"""
    from dubforge.utils.srt import SubLine, parse_srt, write_srt
    rows = []
    for index, line in enumerate(parse_srt(source), 1):
        if line.end <= start or line.start >= end:
            continue
        rows.append(SubLine(index, max(0.0, line.start - start),
                            max(0.0, line.end - start), line.text))
    write_srt(rows, target)


def _input(path: Path, start: float | None, duration: float | None) -> list[str]:
    """试合成用输入精准裁剪；正式合成保持原命令。"""
    if start is None or duration is None:
        return ["-i", str(path)]
    return ["-ss", f"{start:.6f}", "-t", f"{duration:.6f}", "-i", str(path)]


def _mux(ctx, pdir: Path, video: Path, dub: Path, out: Path, total=0.0, *,
         start: float | None = None, burn: bool = False,
         progress_range: tuple[float, float] = (40.0, 99.0), comment_plan=None) -> None:
    """阶段 B: ffmpeg 封装 (配音混音/背景/ducking/烧录)；试合成输出短片段。"""
    bgm_vol = float(ctx.cfg("assemble.bgm_volume", 1.0))
    voice_vol = float(ctx.cfg('assemble.voice_volume', 1.0))
    bg = ctx.upstream("preprocess", "background.wav") if bgm_vol > 0 else None
    ducking = bool(ctx.cfg("assemble.ducking", True))
    crf = int(ctx.cfg("assemble.crf", 18))
    has_bg = bg is not None
    preview = start is not None
    duration = max(0.0, float(total))

    cmd = ["ffmpeg", "-y", "-progress", "pipe:1", "-nostats",
           *_input(video, start, duration if preview else None), "-i", str(dub)]
    filters = [f'[1:a]volume={voice_vol}[voice]']
    if has_bg:
        bg_input = cmd.count("-i")
        cmd += _input(bg, start, duration if preview else None)
        if ducking:
            # 背景作为主输入被人声 (sidechain) 压低: 说话时背景自动避让
            filters.append(
                f"[{bg_input}:a]volume={bgm_vol}[bg];"
                f"[voice]asplit[trigger][speech];[bg][trigger]sidechaincompress=threshold=0.02:ratio=8:attack=60:release=450[duck];"
                f"[speech][duck]amix=inputs=2:duration=first:normalize=0[aout]")
        else:
            filters.append(
                f"[{bg_input}:a]volume={bgm_vol}[bg];"
                f"[voice][bg]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        filters.append("[voice]anull[aout]")

    preview_srt = None
    video_filters = ['setpts=PTS-STARTPTS'] if preview else []
    if burn:
        from dubforge.pipeline.subtitle_style import prepare_burn_filter
        srt = ctx.upstream("subtitles", ctx.cfg("assemble.subtitle_track", "trans") + ".srt")
        # 字幕滤镜路径转义繁琐 → 以 srt 目录为工作目录用相对文件名
        graph, preview_srt = prepare_burn_filter(ctx, video, srt, start if preview else None, duration)
        video_filters.append(graph)
    cwd = srt.parent if burn else pdir / "output"
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        comment_filter, comment_file = prepare_filter(comment_plan, video, cwd,
                                                     start=start or 0, duration=duration)
    except Exception:
        if preview_srt:
            preview_srt.unlink(missing_ok=True)
        raise
    if comment_filter:
        video_filters.append(comment_filter)
    if video_filters:
        filters.append(f"[0:v]{','.join(video_filters)}[v]")
        cmd += ["-map", "[v]", "-c:v", "libx264", "-crf", str(crf), "-preset", "medium"]
    else:
        cmd += ["-map", "0:v", "-c:v", "copy"]
    cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    cmd += ["-filter_complex", ";".join(filters), out.name]
    # 先写临时名, 成功才转正 —— 中断/取消不留下半成品 mp4
    out_dir = pdir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_name(f".tmp_{out.name}")
    cmd[cmd.index(out.name)] = tmp_out.name
    tmp_path = (cwd / tmp_out.name) if cwd != out_dir else tmp_out
    progress = (RateProgress(ctx, duration, start=progress_range[0],
                             end=progress_range[1], scope="phase")
                if duration > 0 else None)

    def on_progress(line):
        if progress and line.startswith("out_time_us="):
            try:
                progress.update(float(line.partition("=")[2]) / 1_000_000)
            except ValueError:
                pass

    try:
        rc, _, err = ctx.run_subprocess(cmd, cwd=cwd, on_stdout=on_progress)
        if rc != 0:
            raise DubForgeError("成片封装失败 (ffmpeg)", code="FFMPEG_FAIL",
                                detail=err[-1500:])
        atomic_replace(tmp_path, out)
    finally:
        tmp_path.unlink(missing_ok=True)
        if preview_srt is not None:
            preview_srt.unlink(missing_ok=True)
        if comment_file:
            comment_file.unlink(missing_ok=True)


# ---- 成片组合 (结算页): 原片组合 = 源视频本身, 永不重复合成 ----
VARIANT_PREFIX = {key: f'final_{key}_' for key in OUTPUT_VARIANTS}


def _default_variants(ctx) -> list[str]:
    """无组合参数时沿用旧配置语义: burn_subtitles 决定配音版是否烧录。"""
    return default_variants(ctx.cfg)


def _outputs_variants(ctx) -> list[str]:
    """结算页任务: 校验并去重要合成的组合清单。"""
    variants = validate_variants(ctx.task_data.get("variants"))
    if ctx.task_data.get('comment_overlay') and ctx.task_data.get('output_schema') != 2:
        # 兼容升级前保存的整批评论任务，不改变恢复执行时的原始意图。
        variants = list(dict.fromkeys(v if v.endswith('_comments') else v + '_comments' for v in variants))
    return variants


def _has_audio_stream(path: Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", check=False)
    return bool(r.stdout.strip())


def _mux_original_sub(ctx, video: Path, out: Path, total: float,
                      progress_range: tuple[float, float], *, burn=True, start=None, comment_plan=None) -> None:
    """原声组合：不依赖配音；试片与完整成品共用字幕/评论渲染。"""
    from dubforge.pipeline.subtitle_style import prepare_burn_filter
    srt = ctx.upstream("subtitles", ctx.cfg("assemble.subtitle_track", "trans") + ".srt") if burn else None
    crf = int(ctx.cfg("assemble.crf", 18))
    cwd = srt.parent if srt else out.parent
    cwd.mkdir(parents=True, exist_ok=True)
    preview_srt = None
    video_filters = ['setpts=PTS-STARTPTS'] if start is not None else []
    if srt:
        graph, preview_srt = prepare_burn_filter(ctx, video, srt, start, total)
        video_filters.append(graph)
    try:
        comment_filter, comment_file = prepare_filter(comment_plan, video, cwd, start=start or 0, duration=total)
    except Exception:
        if preview_srt:
            preview_srt.unlink(missing_ok=True)
        raise
    if comment_filter:
        video_filters.append(comment_filter)
    # 字幕滤镜路径转义繁琐 → 以 srt 目录为工作目录用相对文件名 (与 _mux 一致)
    cmd = ["ffmpeg", "-y", "-progress", "pipe:1", "-nostats", *_input(video, start, total if start is not None else None),
           "-vf", ','.join(video_filters),
           "-map", "0:v"]
    if _has_audio_stream(video):
        cmd += ["-map", "0:a", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", "medium", out.name]

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_name(f".tmp_{out.name}")
    cmd[cmd.index(out.name)] = tmp_out.name
    tmp_path = cwd / tmp_out.name
    progress = (RateProgress(ctx, total, start=progress_range[0],
                             end=progress_range[1], scope="phase")
                if total > 0 else None)

    def on_progress(line):
        if progress and line.startswith("out_time_us="):
            try:
                progress.update(float(line.partition("=")[2]) / 1_000_000)
            except ValueError:
                pass

    try:
        rc, _, err = ctx.run_subprocess(cmd, cwd=cwd, on_stdout=on_progress)
        if rc != 0:
            raise DubForgeError("成片封装失败 (ffmpeg)", code="FFMPEG_FAIL",
                                detail=err[-1500:])
        atomic_replace(tmp_path, out)
    finally:
        tmp_path.unlink(missing_ok=True)
        if comment_file:
            comment_file.unlink(missing_ok=True)
        if preview_srt:
            preview_srt.unlink(missing_ok=True)


def run(ctx) -> None:
    if ctx.task_data.get('version_specs'):
        from dubforge.pipeline.version_render import run
        return run(ctx)
    pdir = ctx.pdir
    from dubforge.pipeline.video_source import resolve_video
    video = resolve_video(ctx.project, pdir)
    if video is None:
        raise DubForgeError("项目没有可用的源视频", code="VIDEO_MISSING",
                            suggestion="本地导入的项目请确认原文件还在原位置; 下载项目请重跑「获取视频」。")

    total = _ffprobe_duration(video)
    if total <= 0:
        raise DubForgeError("无法探测视频时长", code="VIDEO_DURATION")

    plan = ctx.task_data.get('comment_overlay')
    if plan and plan['settings']['start'] >= total:
        raise DubForgeError('评论首次出现时间超过视频时长，请调整上屏方案', code='COMMENTS_OUTSIDE_VIDEO')
    if plan:
        visible = sum(plan['settings']['start'] + i * plan['settings']['interval'] < total for i in range(len(plan['items'])))
        ctx.log(f'评论叠加：视频内可显示 {visible} / {len(plan["items"])} 条，超出结尾的评论不显示。')

    preview = ctx.task_data.get("scope") == "preview"
    if preview:
        start, end = _task_range(ctx, total)
        duration = end - start
        ctx.log(f"试合成区域: {start:.2f}s → {end:.2f}s ({duration:.2f}s)")
        variant = validate_variants(ctx.task_data['variants'])[0] if ctx.task_data.get('variants') else _default_variants(ctx)[0]
        spec = dimensions(variant)
        stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        marker = variant + '_' if ctx.task_data.get('variants') else 'comments_' if plan else ''
        out = pdir / "output" / f"preview_{marker}{stamp}.mp4"
        if spec['dubbing']:
            dub = _mix_timeline(ctx, pdir, total, start=start, end=end)
            ctx.report_progress(40)
            ctx.check_pause()
            _mux(ctx, pdir, video, dub, out, duration, start=start, burn=spec['burn_subtitles'], comment_plan=plan)
        else:
            _mux_original_sub(ctx, video, out, duration, start=start, burn=spec['burn_subtitles'], comment_plan=plan, progress_range=(10, 99))
        ctx.register_file(out.name, out, "video")
        size = out.stat().st_size / 1048576
        ctx.log(f"✅ 试合成片段: {out.name} ({size:.1f} MB) | 区域 {start:.2f}s → {end:.2f}s")
        return

    outputs_scope = ctx.task_data.get("scope") == "outputs"
    variants = (_outputs_variants(ctx) if outputs_scope
                else _default_variants(ctx))
    if any(dimensions(v)['include_comments'] for v in variants) and not plan:
        raise DubForgeError('评论版需要已确认的评论方案', code='COMMENTS_NOT_ENABLED')
    needs_dub = any(dimensions(v)['dubbing'] for v in variants)
    from dubforge.publishing.service import output_replacement
    # 及早提示发布占用；渲染完成后再次在同一门禁内提交，防止中途启动上传。
    with output_replacement(ctx.project.uid):
        pass

    dub = None
    if needs_dub:
        dub = _mix_timeline(ctx, pdir, total)
        ctx.report_progress(40)
        ctx.check_pause()
        ctx.log("人声轨就绪, 开始封装...")

    out_dir = pdir / "output"
    out_dir.mkdir(exist_ok=True)
    # 仅首次生成命名；以后复用同组合的位置，避免无限增加正式成品。
    stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    lo = 40.0 if needs_dub else 10.0
    for index, variant in enumerate(variants):
        ctx.check_cancel()
        ctx.check_pause()
        hi = lo + (99.0 - lo) * (index + 1) / len(variants)
        spec = dimensions(variant)
        comment_plan = plan if spec['include_comments'] else None
        out = current_output(out_dir, variant) or out_dir / f"{VARIANT_PREFIX[variant]}{stamp}.mp4"
        name = out.name
        pending = out.with_name(f'.tmp_pending_{name}')
        replacing = out.exists()
        try:
            if not spec['dubbing']:
                _mux_original_sub(ctx, video, pending, total, progress_range=(lo, hi), burn=spec['burn_subtitles'], comment_plan=comment_plan)
            else:
                _mux(ctx, pdir, video, dub, pending, total=total,
                     burn=spec['burn_subtitles'], progress_range=(lo, hi), comment_plan=comment_plan)
            with output_replacement(ctx.project.uid):
                ctx.check_cancel()
                atomic_replace(pending, out)
                ctx.register_file(name, out, "video", replace_existing=True)
        finally:
            pending.unlink(missing_ok=True)
        size = out.stat().st_size // 1048576
        ctx.log(f"✅ {'已替换' if replacing else '已生成'} {VARIANT_LABELS[variant]}: {name} ({size} MB)")

    if outputs_scope:
        ctx.log("原片（原声 · 不烧录字幕）即源视频，无需合成，直接使用原文件。")
    ctx.log(f"输出目录: {out_dir}")
