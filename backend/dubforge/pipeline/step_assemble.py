"""
步骤 9: assemble —— 成片合成
============================
两阶段:
  A) 铺轨: 批量配音产生的句级音频按时间轴铺到与视频等长的完整人声轨 (audio/dub_full.wav),
     逐句后期增益、保守均衡与淡化；可选测量后静态响度归一
  B) 封装: ffmpeg 混音 —— 背景音量 + 人声 ducking (sidechaincompress,
     说话时自动压低背景) + 可选字幕烧录; 视频流直拷不重编码

产物: 每种组合一个当前成品，保留首次命名，重跑成功后原子替换。
外挂字幕由字幕导出步骤单独提供。
"""
import datetime
import json
import math
import subprocess
from pathlib import Path

from dubforge.errors import DubForgeError
from dubforge.utils.ffmpeg import complex_filter_command
from dubforge.pipeline.assembly_outputs import OUTPUT_VARIANTS, VARIANT_LABELS, default_variants, validate_variants, dimensions, current_output
from dubforge.progress import RateProgress
from dubforge.execution_progress import report_stage
from dubforge.utils import audio as au
from dubforge.utils.fs import atomic_replace
from dubforge.pipeline.comment_overlay import prepare_filter
from dubforge.pipeline.dubbing_placement import build_plan, region_entries, require_video_fit
from dubforge.pipeline.render_resources import bounded_ffmpeg_command, ffmpeg_failure, preflight_render


def _ffprobe_duration(path: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", check=False, timeout=15)
        seconds = float(r.stdout.strip())
        return seconds if math.isfinite(seconds) and seconds > 0 else 0.0
    except (OSError, ValueError, subprocess.TimeoutExpired):
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
    if end - start < 0.2 - 1e-9:
        raise DubForgeError("试合成区域太短，至少需要 0.2 秒", code="ASSEMBLE_PREVIEW_RANGE")
    return round(start, 3), round(min(end, total), 3)


def _require_dubbing(ctx):
    from dubforge.pipeline.dependencies import Dependencies
    deps = Dependencies(ctx.project)
    # 使用任务冻结的配置；不让运行中的全局配置改变本次校验语义。
    deps.cfg = ctx.cfg
    return deps.require_dubbing()


def _apply_adaptive_voice_timing(ctx, pdir: Path, inputs: list[dict]) -> list[dict]:
    """按当前选槽即时排布，旧报告缺失或局部冲突不再使全片回退。"""
    from dubforge.pipeline import voice_timing
    layout = voice_timing.plan(ctx.project, pdir, inputs, ctx.cfg,
                               video_duration=getattr(ctx, '_voice_video_duration', None))
    ctx._voice_layout = layout
    if layout['timing']['shifted']:
        ctx.log(f"✅ 自适应排布：{layout['timing']['shifted']} 句利用相邻空位，原文、译文与配音共用时间轴")
    return layout['inputs']


def _mix_timeline(ctx, pdir: Path, total: float, *,
                  start: float | None = None, end: float | None = None, output_name=None, register=True) -> Path:
    """铺轨失败时清理本次试片音频；成功后的任务级清理由版本执行器接管。"""
    manifest = (getattr(ctx, 'version', None) or {}).get('render_manifest')
    if manifest is not None:
        from dubforge.pipeline.assembly_manifest import mix_voice
        return mix_voice(ctx, manifest, pdir / 'audio' / (output_name or 'dub_full.wav'), total)
    created = []
    try:
        return _mix_timeline_impl(ctx, pdir, total, start=start, end=end,
                                  output_name=output_name, register=register, _created=created)
    except BaseException as exc:
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if getattr(ctx, '_preview_full_voice', None) in created:
            del ctx._preview_full_voice
        if isinstance(exc, (MemoryError, OSError)):
            failure = ffmpeg_failure(exc, output_dir=pdir / 'audio')
            if failure.code in {'ASSEMBLE_DISK_SPACE', 'ASSEMBLE_MEMORY_PRESSURE'}:
                raise failure from exc
        raise


def _mix_timeline_impl(ctx, pdir: Path, total: float, *,
                       start=None, end=None, output_name=None, register=True, _created=None) -> Path:
    """阶段 A: 按时间轴铺轨 + 归一；试合成时只为选定区域生成独立音频。"""
    from dubforge.pipeline.step_dubtask import build_task
    from dubforge.pipeline.dubbing_edit import production_lines, take_name
    task = build_task(ctx)
    _require_dubbing(ctx)
    lines = production_lines(ctx.project, task['lines'], pdir)
    preview = start is not None and end is not None
    region_start = float(start or 0.0)
    region_end = float(end if end is not None else total)
    # 声音轨为 32k 单声道浮点 WAV；预览统一响度还会暂存一份完整人声。
    copies = 2 if preview and ctx.cfg('assemble.final_loudness_mode', 'off') != 'off' else 1
    voice_bytes = int(max(0., total) * au.SR * 4 * copies
                      + (max(0., region_end - region_start) * au.SR * 4 if preview else 0)) + 65536
    preflight_render(ctx, pdir / 'audio', required_bytes=voice_bytes)
    from dubforge.pipeline.tts_duration import audio_seconds
    paths = {}
    inputs = []
    for index, line in enumerate(lines):
        ctx.check_cancel()
        report_stage(ctx, 'checking_voice_tracks', '核对选用配音与实际时长',
                     completed=index, total=len(lines), unit='句', current_id=line['id'])
        source = line['source'] if line['source'] == 'original' or line['take'] is not None else 'silent'
        seconds = None
        if source == 'dub':
            wav = ctx.upstream('tts', take_name(line['id'], line['take']))
            paths[line['id']] = wav
            seconds = audio_seconds(wav)
            if seconds is None:
                raise DubForgeError(f"无法确认第 {line['id']} 句所选配音的时长，合成已停止以免遗漏声音",
                                    code='TTS_AUDIO_INVALID',
                                    suggestion='请在配音页试听或重新生成这句配音，再合成成片。')
        inputs.append({**line, 'source': source, 'seconds': seconds})
    ctx._voice_video_duration = total
    inputs = _apply_adaptive_voice_timing(ctx, pdir, inputs)
    report_stage(ctx, 'placing_voices', '核对人声排布与相邻冲突')
    placement = build_plan(inputs, total)
    if not preview:
        require_video_fit(placement)
        unresolved = ctx._voice_layout['timing'].get('unresolved_ids', [])
        if (ctx._voice_layout['timing']['enabled'] or ctx._voice_layout['timing'].get('manual')) and unresolved:
            raise DubForgeError('局部排布后仍有配音无法容纳，已停止成片以避免人声重叠：第 '
                                + '、'.join(map(str, unresolved[:12])) + ' 句',
                                code='DUB_TIMING_UNRESOLVED',
                                suggestion='请在配音页查看冲突窗口，为这些句子生成新槽并启用超长缩短重试，或调整译文与排布范围。')
    rows = {row['id']: row for row in placement['lines']}
    collisions = [issue for issue in placement['issues'] if issue['kind'] == 'overlap'
                  and max(rows[issue['id']]['start'], rows[issue['next_id']]['start'], region_start)
                  < min(rows[issue['id']]['audio_end'], rows[issue['next_id']]['audio_end'], region_end)]
    for issue in collisions[:12]:
        ctx.log(f"⚠ 第 {issue['id']} 句与第 {issue['next_id']} 句人声重叠 {issue['excess']:.2f}s；"
                '保留源时间与完整声音，请在配音页试听并调整。')
    if len(collisions) > 12:
        ctx.log(f'⚠ 另有 {len(collisions) - 12} 处人声重叠，请在配音页核对。')
    entries = []
    entry_lines = []
    original = None
    from dubforge.pipeline import audio_post
    from dubforge.pipeline.dubbing_edit import file_version
    saved_gains = ((getattr(ctx, 'version', None) or {}).get('audio_line_gains')
                   if isinstance(getattr(ctx, 'version', None), dict) else None)
    line_lookup = {row['id']: row for row in inputs}
    # 试片也以完整人声轨测量和均衡，避免同一句因试片边界而改变响度。
    for line in region_entries(placement, 0, total):
        ctx.check_cancel()
        samples = None
        audio_version = None
        if line['source'] == 'original':
            if original is None:
                original = au.load_mono_32k(ctx.upstream('preprocess', 'vocal.wav'))
            sample_start = round(line['clip_start'] * au.SR)
            sample_end = round(line['clip_end'] * au.SR)
            samples = original[sample_start:sample_end]
            audio_version = file_version(ctx.upstream('preprocess', 'vocal.wav'))
        elif line['source'] == 'dub':
            wav = paths[line['id']]
            audio = au.load_mono_32k(wav)
            sample_start = round(line['audio_offset'] * au.SR)
            sample_end = round((line['audio_offset'] + line['clip_end'] - line['clip_start']) * au.SR)
            samples = audio[sample_start:sample_end]
            audio_version = file_version(wav)
        if samples is not None:
            state = audio_post.line_state(ctx.project, line_lookup[line['id']],
                                          audio_version=audio_version, saved=saved_gains)
            entries.append((line['offset'], samples))
            entry_lines.append(dict(id=line['id'], start=line['clip_start'], end=line['clip_end'],
                                   gain_db=state['gain_db'], gain_stale=state['stale']))

    duration = total
    report_stage(ctx, 'mixing_voice', '混合逐句配音与原声人声轨', total=len(entries), unit='句')
    # 全部选择空位也有明确语义：保留视频与背景，人声轨为静音。
    ctx.log(f"铺轨: 区域内 {len(entries)} 句 → {duration:.1f}s 人声轨"
            if preview else f"铺轨: {len(entries)} 句 → {total:.1f}s 人声轨")
    # 句首尾 10ms 淡入淡出, 消除逐句拼接处的咔哒声 (au.fade_in_out)
    entries = [(offset, au.fade_in_out(audio)) for offset, audio in entries]
    entries, levels, corrections = audio_post.balance_entries(entries,
        enabled=bool(ctx.cfg('assemble.sentence_leveling', False)),
        max_db=ctx.cfg('assemble.sentence_leveling_max_db', 3.))
    # 自动均衡先作用，人工修正最后叠加，不能由均衡抵消用户的精调。
    entries = [(offset, samples if not line['gain_db'] else samples * 10 ** (line['gain_db'] / 20))
               for (offset, samples), line in zip(entries, entry_lines)]
    ctx._audio_line_report = [{**line, 'active_rms_db': level, 'leveling_gain_db': correction}
                              for line, level, correction in zip(entry_lines, levels, corrections)]
    from dubforge.pipeline.audio_quality import annotate_lines
    ctx._audio_line_report = annotate_lines(ctx._audio_line_report, [audio for _, audio in entries])
    mixed = au.mix_over_timeline(entries, duration)
    if not ctx.cfg('assemble.loudnorm', True):
        mixed = au.peak_normalize(mixed, -3.0)

    stamp = datetime.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    out = (pdir / "audio" / f"preview_dub_{stamp}.wav" if preview
           else pdir / "audio" / "dub_full.wav")
    if output_name:
        out = pdir / 'audio' / output_name
    if _created is not None and (preview or out.name.startswith('.tmp_')):
        _created.append(out)
    out.parent.mkdir(exist_ok=True)
    import soundfile as sf
    tmp = out.with_suffix(".wav.tmp")
    # .tmp 后缀无法推断格式, 显式指定 WAV (与 ffmpeg 原子写同理)
    try:
        sf.write(str(tmp), mixed, au.SR, subtype="FLOAT", format="WAV")
        atomic_replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)

    # 落盘后释放整轨与逐句样本，避免与随后的测量/编码同时占用大数组。
    del mixed, entries, original

    if ctx.cfg("assemble.loudnorm", True):
        report_stage(ctx, 'normalizing_loudness', '统一人声响度')
        from dubforge.pipeline.audio_quality import normalize_voice
        normalize_voice(out, ctx)
        ctx.log('人声整轨测量与静态响度调整完成（目标 −16 LUFS，保留声音起伏）')
    if preview:
        with sf.SoundFile(out) as stream:
            rate = stream.samplerate
            stream.seek(round(region_start * rate))
            data = stream.read(round((region_end - region_start) * rate), dtype='float32')
        if ctx.cfg('assemble.final_loudness_mode', 'off') != 'off':
            full = out.with_name('.tmp_full_' + out.name)
            if _created is not None:
                _created.append(full)
            # 写一次完整浮点人声，供整片混音测量；不编码整片画面。
            import shutil
            shutil.copyfile(out, full)
            ctx._preview_full_voice = full
        tmp = out.with_suffix('.wav.tmp')
        try:
            sf.write(str(tmp), data, rate,
                     subtype='FLOAT', format='WAV')
            atomic_replace(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
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


def _output_cut_plan(ctx):
    """仅使用执行边界冻结的剪辑，不在编码中读取用户刚保存的下次方案。"""
    version = getattr(ctx, 'version', None)
    if isinstance(version, dict) and 'output_cuts' in version:
        return version['output_cuts']
    return (getattr(ctx, 'task_data', None) or {}).get('output_cuts')


def _mixed_audio(ctx, video, dub, duration, *, start=None):
    """正式渲染与整片声音测量共用同一混音图。"""
    from dubforge.pipeline.content_redaction import audio_plan
    bgm_vol = float(ctx.cfg("assemble.bgm_volume", 1.0))
    voice_vol = float(ctx.cfg('assemble.voice_volume', 1.0))
    ducking = bool(ctx.cfg("assemble.ducking", True))
    preview = start is not None
    audio = audio_plan(ctx, start=start or 0.0, duration=duration)
    if audio['ranges'] and audio['requested_mode'] == 'voice_only':
        bg = audio['background']
    else:
        bg = ctx.upstream("preprocess", "background.wav") if bgm_vol > 0 else None
    has_bg = bg is not None
    if audio['fallback_reason']:
        ctx.log(audio['fallback_reason'])

    cmd = ["ffmpeg", "-y", "-progress", "pipe:1", "-nostats",
           *_input(video, start, duration if preview else None), "-i", str(dub)]
    # 人声先屏蔽再触发背景避让，命中段的独立背景自然保留。
    voice_filter = (',' + audio['filter']) if audio['filter'] and audio['effective_mode'] == 'voice_only' else ''
    filters = [f'[1:a]anull{voice_filter}[voice_raw]']
    if has_bg:
        bg_input = cmd.count("-i")
        cmd += _input(bg, start, duration if preview else None)
        if ducking and voice_vol > 0:
            # 背景作为主输入被人声 (sidechain) 压低: 说话时背景自动避让
            filters.append(
                f"[{bg_input}:a]volume={bgm_vol}[bg];"
                f"[voice_raw]asplit[trigger][speech_raw];[speech_raw]volume={voice_vol}[speech];[bg][trigger]sidechaincompress=threshold=0.02:ratio=8:attack=60:release=450[duck];"
                f"[speech][duck]amix=inputs=2:duration=first:normalize=0[aout]")
        else:
            filters.append(
                f"[{bg_input}:a]volume={bgm_vol}[bg];"
                f"[voice_raw]volume={voice_vol}[speech];[speech][bg]amix=inputs=2:duration=first:normalize=0[aout]")
    else:
        filters.append(f"[voice_raw]volume={voice_vol}[aout]")
    if audio['filter'] and audio['effective_mode'] != 'voice_only':
        mute = audio['filter']
        filters.append(f'[aout]{mute}[reviewed_audio]')
        audio_output = '[reviewed_audio]'
    else:
        audio_output = '[aout]'

    return cmd, filters, audio_output


def _master_input(video, master, *, start, duration, cut_plan):
    """统一响度先处理完整声音；短片只读取对应成品位置，不再独立归一。"""
    from dubforge.pipeline.output_cuts import keep_segments
    source_start = start or 0.
    selected = keep_segments(cut_plan, start=source_start, end=source_start + duration)
    length = sum(row['end'] - row['start'] for row in selected)
    offset = sum(row['end'] - row['start'] for row in keep_segments(cut_plan, start=0., end=source_start)) if source_start > 0 else 0.
    cmd = ['ffmpeg', '-y', '-progress', 'pipe:1', '-nostats',
           *_input(video, start, duration if start is not None else None),
           *_input(Path(master['path']), offset if start is not None else None,
                   length if start is not None else None)]
    return cmd, ['[1:a:0]anull[master_audio]'], '[master_audio]'


def _compare_audio(ctx, report, video, *, start, duration, full_duration):
    from dubforge.pipeline.audio_comparison import augment_report
    return augment_report(ctx, report, video, start=start, duration=duration,
                          full_duration=full_duration, cut_plan=_output_cut_plan(ctx))


def _write_audio_candidate(ctx, cmd, filters, label, out, duration, *, treatment=None, mastered=False):
    """局部替换先准备一次完整 PCM，避免每个片段重复混音或拼接 AAC。"""
    from dubforge.pipeline.audio_master import _graph
    graph, length = _graph(filters, label, None if mastered else _output_cut_plan(ctx), duration)
    label = '[ammix]'
    if treatment is not None and not mastered:
        from dubforge.pipeline.audio_quality import gain_filter
        addition, label = gain_filter(label, treatment)
        graph.append(addition)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pending = out.with_name('.tmp_' + out.name)
    command = [*cmd, '-filter_complex', ';'.join(graph), '-map', label, '-vn',
               '-c:a', 'pcm_f32le', '-ar', '48000', '-ac', '2', '-t', f'{length:.6f}',
               '-f', 'wav', str(pending)]
    report_stage(ctx, 'preparing_partial_audio', '准备局部替换所需的完整音轨')
    try:
        with complex_filter_command(bounded_ffmpeg_command(command), out.parent) as prepared:
            code, _, error = ctx.run_subprocess(prepared, cwd=out.parent)
        if code:
            raise ffmpeg_failure(error, output_dir=out.parent)
        ctx.check_cancel()
        atomic_replace(pending, out)
    finally:
        pending.unlink(missing_ok=True)


def _mux(ctx, pdir: Path, video: Path, dub: Path, out: Path, total=0.0, *,
         start: float | None = None, burn: bool = False,
         progress_range: tuple[float, float] = (40.0, 99.0), comment_plan=None,
         audio_only: bool = False) -> None:
    """阶段 B: ffmpeg 封装 (配音混音/背景/ducking/烧录)；试合成输出短片段。"""
    from dubforge.pipeline.content_redaction import audio_plan, safe_overlay
    comment_plan = safe_overlay(ctx, comment_plan)
    preview = start is not None
    duration = max(0.0, float(total))
    preflight_render(ctx, out.parent)
    cmd, filters, audio_output = _mixed_audio(ctx, video, dub, duration, start=start)
    reference = None
    treatment = None
    full_voice = getattr(ctx, "_preview_full_voice", None)
    mastered = False
    full_duration = _ffprobe_duration(video) if preview else duration
    if ctx.cfg("assemble.final_loudness_mode", "off") != "off":
        from dubforge.pipeline.audio_quality import measure_mix, final_gain
        reference_cmd, reference_filters, reference_label = _mixed_audio(ctx, video, full_voice if preview else dub, full_duration)
        try:
            if ctx.cfg('assemble.final_loudness_mode', 'off') == 'uniform':
                from dubforge.pipeline.audio_master import prepare_master
                master = prepare_master(ctx, reference_cmd, reference_filters, reference_label,
                                        cut_plan=_output_cut_plan(ctx), duration=full_duration)
                reference, treatment = master['reference'], master['processing']
                cmd, filters, audio_output = _master_input(video, master, start=start, duration=duration,
                                                          cut_plan=_output_cut_plan(ctx))
                mastered = True
            else:
                reference = measure_mix(ctx, reference_cmd, reference_filters, reference_label,
                                        cut_plan=_output_cut_plan(ctx), duration=full_duration)
                treatment = final_gain(ctx, reference)
        finally:
            if full_voice:
                full_voice.unlink(missing_ok=True)

    if audio_only:
        _write_audio_candidate(ctx, cmd, filters, audio_output, out, duration,
                               treatment=treatment, mastered=mastered)
        return

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
        from dubforge.pipeline.subtitle_style import render_canvas
        comment_filter, comment_file = prepare_filter(comment_plan, video, cwd,
                                                     start=start or 0, duration=duration,
                                                     canvas=render_canvas(ctx, video) if comment_plan else None)
    except Exception:
        if preview_srt:
            preview_srt.unlink(missing_ok=True)
        raise
    if comment_filter:
        video_filters.append(comment_filter)
    cut_plan = _output_cut_plan(ctx)
    output_duration = duration
    video_output = '[v]' if video_filters or cut_plan else '0:v'
    if video_filters or cut_plan:
        filters.append(f"[0:v]{','.join(video_filters) if video_filters else 'null'}[v]")
    if cut_plan:
        from dubforge.pipeline.output_cuts import filter_graph
        graph, video_output, cut_audio, output_duration = filter_graph(
            cut_plan, video_output, None if mastered else audio_output, start=start or 0, duration=duration)
        if not mastered:
            audio_output = cut_audio
        if graph:
            filters.append(graph)
        cmd += ['-map_chapters', '-1']
    if treatment is not None and not mastered:
        from dubforge.pipeline.audio_quality import gain_filter
        graph, audio_output = gain_filter(audio_output, treatment)
        filters.append(graph)
    if video_filters or cut_plan:
        from dubforge.pipeline.video_encoding import video_arguments
        cmd += ["-map", video_output, *video_arguments(ctx)]
    else:
        cmd += ["-map", "0:v", "-c:v", "copy"]
    cmd += ["-map", audio_output, "-c:a", "aac", "-b:a", "192k"]
    if cut_plan:
        from dubforge.pipeline.output_cuts import mux_timing_options
        # concat 帧时长未知时 -shortest 会依据早一帧的错误终点截短音频。
        cmd += mux_timing_options(output_duration)
    else:
        cmd += ['-shortest']
    cmd += ["-filter_complex", ";".join(filters), out.name]
    # 先写临时名, 成功才转正 —— 中断/取消不留下半成品 mp4
    out_dir = pdir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_name(f".tmp_{out.name}")
    cmd[cmd.index(out.name)] = tmp_out.name
    tmp_path = (cwd / tmp_out.name) if cwd != out_dir else tmp_out
    progress = None

    def on_ready():
        nonlocal progress
        progress = (RateProgress(ctx, output_duration, start=progress_range[0],
                                 end=progress_range[1], scope="phase") if output_duration > 0 else None)
        report_stage(ctx, 'encoding', '编码成片画面、音频与字幕', completed=0,
                     total=round(output_duration, 2), unit='成片秒')

    def on_progress(line):
        if progress and line.startswith("out_time_us="):
            try:
                seconds = float(line.partition("=")[2]) / 1_000_000
                progress.update(seconds)
                report_stage(ctx, 'encoding', '编码成片画面、音频与字幕',
                             completed=round(min(output_duration, max(0., seconds)), 2),
                             total=round(output_duration, 2), unit='成片秒')
            except ValueError:
                pass

    try:
        from dubforge.pipeline.video_encoding import run as encode_video
        try:
            rc, _, err = encode_video(ctx, video, cmd, cwd=cwd,
                required=bool(video_filters or cut_plan), on_stdout=on_progress, on_ready=on_ready)
        except OSError as exc:
            raise ffmpeg_failure(exc, output_dir=out.parent) from exc
        if rc != 0:
            raise ffmpeg_failure(err, output_dir=out.parent)
        from dubforge.pipeline.audio_quality import finalize, write_report
        ctx._audio_report = finalize(ctx, tmp_path, line_report=getattr(ctx, '_audio_line_report', None),
                                     reference=reference, applied=treatment, preview=preview)
        ctx._audio_report = _compare_audio(ctx, ctx._audio_report, video, start=start,
                                          duration=duration, full_duration=full_duration)
        atomic_replace(tmp_path, out)
        write_report(out, ctx._audio_report)
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


def _first_audio_stream(path: Path) -> dict | None:
    """一次读取实际使用的首条音轨；未知编码仍视为有声，交由 AAC 转码。"""
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                                 "-show_entries", "stream=index,codec_name", "-of", "json", str(path)],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", check=False, timeout=15)
        if result.returncode:
            raise ValueError('ffprobe failed')
        streams = json.loads(result.stdout)['streams']
        if not isinstance(streams, list) or any(not isinstance(row, dict) for row in streams):
            raise ValueError('invalid audio streams')
        return streams[0] if streams else None
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
        # 探测失败不能当作无声，否则会静默丢失原音轨。
        raise DubForgeError('无法读取源视频音轨信息，请确认文件完整且 FFmpeg 可用',
                            code='AUDIO_STREAM_PROBE_FAILED') from exc


def _has_audio_stream(path: Path) -> bool:
    return _first_audio_stream(path) is not None


def _mux_original_sub(ctx, video: Path, out: Path, total: float,
                      progress_range: tuple[float, float], *, burn=True, start=None, comment_plan=None,
                      audio_only: bool = False, video_only: bool = False) -> None:
    """原声组合：不依赖配音；试片与完整成品共用字幕/评论渲染。"""
    preflight_render(ctx, out.parent)
    if audio_only:
        burn, comment_plan = False, None
    audio_stream = None if video_only else _first_audio_stream(video)
    has_audio = audio_stream is not None
    from dubforge.pipeline.content_redaction import audio_plan, apply_audio_review, safe_overlay
    comment_plan = safe_overlay(ctx, comment_plan)
    from dubforge.pipeline.subtitle_style import prepare_burn_filter, render_canvas
    srt = ctx.upstream("subtitles", ctx.cfg("assemble.subtitle_track", "trans") + ".srt") if burn else None
    cwd = srt.parent if srt else out.parent
    cwd.mkdir(parents=True, exist_ok=True)
    preview_srt = None
    video_filters = ['setpts=PTS-STARTPTS'] if start is not None else []
    if srt:
        graph, preview_srt = prepare_burn_filter(ctx, video, srt, start, total)
        video_filters.append(graph)
    try:
        comment_filter, comment_file = prepare_filter(comment_plan, video, cwd, start=start or 0, duration=total,
                                                     canvas=render_canvas(ctx, video) if comment_plan else None)
    except Exception:
        if preview_srt:
            preview_srt.unlink(missing_ok=True)
        raise
    if comment_filter:
        video_filters.append(comment_filter)
    # 字幕滤镜路径转义繁琐 → 以 srt 目录为工作目录用相对文件名 (与 _mux 一致)
    cmd = ["ffmpeg", "-y", "-progress", "pipe:1", "-nostats", *_input(video, start, total if start is not None else None)]
    reference = None
    treatment = None
    mastered = False
    full_duration = total if start is None else _ffprobe_duration(video)
    if has_audio and ctx.cfg('assemble.final_loudness_mode', 'off') != 'off':
        from dubforge.pipeline.audio_quality import measure_mix, final_gain
        full_audio = audio_plan(ctx, start=0., duration=full_duration)
        reference_cmd = ['ffmpeg', '-hide_banner', '-nostats', '-i', str(video)]
        reference_bg = None
        if full_audio['background'] is not None:
            reference_cmd += ['-i', str(full_audio['background'])]
            reference_bg = '1:a:0'
        reference_filters = ([apply_audio_review(full_audio, '0:a:0', background_label=reference_bg)]
                             if full_audio['filter'] else ['[0:a:0]anull[reviewed_audio]'])
        if ctx.cfg('assemble.final_loudness_mode', 'off') == 'uniform':
            from dubforge.pipeline.audio_master import prepare_master
            master = prepare_master(ctx, reference_cmd, reference_filters, '[reviewed_audio]',
                                    cut_plan=_output_cut_plan(ctx), duration=full_duration)
            reference, treatment = master['reference'], master['processing']
            cmd, master_filters, master_output = _master_input(video, master, start=start, duration=total,
                                                               cut_plan=_output_cut_plan(ctx))
            mastered = True
        else:
            reference = measure_mix(ctx, reference_cmd, reference_filters, '[reviewed_audio]',
                                    cut_plan=_output_cut_plan(ctx), duration=full_duration)
            treatment = final_gain(ctx, reference)
    audio = audio_plan(ctx, start=start or 0.0, duration=total) if has_audio else None
    background_label = None
    if audio and audio['background'] is not None and not mastered:
        cmd += _input(audio['background'], start, total if start is not None else None)
        background_label = '1:a:0'
    cut_plan = _output_cut_plan(ctx)
    if audio_only:
        if not has_audio:
            cmd += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
            filters, audio_output = ['[1:a:0]anull[reviewed_audio]'], '[reviewed_audio]'
        elif mastered:
            filters, audio_output = master_filters, master_output
        else:
            filters = [(apply_audio_review(audio, '0:a:0', background_label=background_label)
                        if audio['filter'] else '[0:a:0]anull[reviewed_audio]')]
            audio_output = '[reviewed_audio]'
        _write_audio_candidate(ctx, cmd, filters, audio_output, out, total,
                               treatment=treatment, mastered=mastered)
        return
    # 只复制无需处理的完整 AAC 音轨；试片保持解码裁剪，避免包边界造成偏移。
    copy_audio = bool(has_audio and start is None and not cut_plan and not mastered
                      and not audio['filter'] and treatment is None
                      and ctx.cfg('assemble.final_loudness_mode', 'off') == 'off'
                      and audio_stream.get('codec_name') == 'aac')
    if has_audio:
        if copy_audio:
            ctx.log('原声音轨：直接复制 AAC，不重复编码；画面仍按字幕与评论设置处理。')
        else:
            reasons = []
            if start is not None:
                reasons.append('精准截取试片')
            if cut_plan:
                reasons.append('成片剪辑')
            if audio['filter']:
                reasons.append('声音屏蔽')
            if ctx.cfg('assemble.final_loudness_mode', 'off') != 'off':
                reasons.append('成片响度处理')
            if audio_stream.get('codec_name') != 'aac':
                reasons.append('源音轨不是已确认可直接复制的 AAC')
            ctx.log('原声音轨：编码为 AAC 192 kb/s（' + '、'.join(reasons) + '）。')
    output_duration = total
    if cut_plan:
        from dubforge.pipeline.output_cuts import filter_graph
        filters = [f"[0:v]{','.join(video_filters) if video_filters else 'null'}[v]"]
        audio_output = master_output if mastered else '0:a:0' if has_audio else None
        if mastered:
            filters.extend(master_filters)
        if audio and audio['filter'] and not mastered:
            filters.append(apply_audio_review(audio, '0:a:0', background_label=background_label))
            audio_output = '[reviewed_audio]'
        graph, video_output, cut_audio, output_duration = filter_graph(
            cut_plan, '[v]', None if mastered else audio_output, start=start or 0, duration=total)
        if not mastered:
            audio_output = cut_audio
        if graph:
            filters.append(graph)
        if audio_output and treatment is not None and not mastered:
            from dubforge.pipeline.audio_quality import gain_filter
            graph, audio_output = gain_filter(audio_output, treatment)
            filters.append(graph)
        cmd += ['-filter_complex', ';'.join(filters), '-map', video_output, '-map_chapters', '-1']
        if audio_output:
            cmd += ['-map', audio_output, '-c:a', 'aac', '-b:a', '192k']
        if audio and audio['fallback_reason']:
            ctx.log(audio['fallback_reason'])
    else:
        if video_filters:
            cmd += ['-vf', ','.join(video_filters)]
        cmd += ['-map', '0:v']
    if has_audio and not cut_plan:
        if audio['fallback_reason']:
            ctx.log(audio['fallback_reason'])
        if mastered:
            cmd += ['-filter_complex', ';'.join(master_filters), '-map', master_output]
        elif audio['filter'] or treatment is not None:
            graph = (apply_audio_review(audio, '0:a:0', background_label=background_label)
                     if audio['filter'] else '[0:a:0]anull[reviewed_audio]')
            audio_output = '[reviewed_audio]'
            if treatment is not None:
                from dubforge.pipeline.audio_quality import gain_filter
                addition, audio_output = gain_filter(audio_output, treatment)
                graph += ';' + addition
            cmd += ['-filter_complex', graph, '-map', audio_output]
        else:
            cmd += ['-map', '0:a:0']
        cmd += ['-c:a', 'copy'] if copy_audio else ['-c:a', 'aac', '-b:a', '192k']
    from dubforge.pipeline.video_encoding import video_arguments
    cmd += video_arguments(ctx, required=bool(video_filters or cut_plan))
    if cut_plan:
        from dubforge.pipeline.output_cuts import mux_timing_options
        cmd += mux_timing_options(output_duration)
    cmd += [out.name]
    encoding_label = '编码成片画面、音频与字幕'
    if copy_audio:
        encoding_label = '编码画面，直接复制原声音轨' if video_filters else '封装成片，直接复制原声音轨'

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_name(f".tmp_{out.name}")
    cmd[cmd.index(out.name)] = tmp_out.name
    tmp_path = cwd / tmp_out.name
    progress = None

    def on_ready():
        nonlocal progress
        progress = (RateProgress(ctx, output_duration, start=progress_range[0],
                                 end=progress_range[1], scope="phase") if output_duration > 0 else None)
        report_stage(ctx, 'encoding', encoding_label, completed=0,
                     total=round(output_duration, 2), unit='成片秒')

    def on_progress(line):
        if progress and line.startswith("out_time_us="):
            try:
                seconds = float(line.partition("=")[2]) / 1_000_000
                progress.update(seconds)
                report_stage(ctx, 'encoding', encoding_label,
                             completed=round(min(output_duration, max(0., seconds)), 2),
                             total=round(output_duration, 2), unit='成片秒')
            except ValueError:
                pass

    try:
        from dubforge.pipeline.video_encoding import run as encode_video
        try:
            rc, _, err = encode_video(ctx, video, cmd, cwd=cwd,
                required=bool(video_filters or cut_plan), on_stdout=on_progress, on_ready=on_ready)
        except OSError as exc:
            raise ffmpeg_failure(exc, output_dir=out.parent) from exc
        if rc != 0:
            raise ffmpeg_failure(err, output_dir=out.parent)
        from dubforge.pipeline.audio_quality import finalize, write_report
        ctx._audio_report = finalize(ctx, tmp_path, has_audio=has_audio, reference=reference,
                                     applied=treatment, preview=start is not None)
        ctx._audio_report = _compare_audio(ctx, ctx._audio_report, video, start=start,
                                          duration=total, full_duration=full_duration)
        atomic_replace(tmp_path, out)
        write_report(out, ctx._audio_report)
    finally:
        tmp_path.unlink(missing_ok=True)
        if comment_file:
            comment_file.unlink(missing_ok=True)
        if preview_srt:
            preview_srt.unlink(missing_ok=True)


def run(ctx) -> None:
    report_stage(ctx, 'preparing_assembly', '读取成片方案与源视频')
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
    report_stage(ctx, 'checking_media', '核对视频时长与合成方案')
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
        report_stage(ctx, 'preparing_output', f'准备成片 {index + 1}/{len(variants)}',
                     completed=index, total=len(variants), unit='成片', batch_index=index + 1, batch_total=len(variants))
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
                report_stage(ctx, 'saving_output', '保存成片与登记结果', batch_index=index + 1, batch_total=len(variants))
                atomic_replace(pending, out)
                from dubforge.pipeline.audio_quality import write_report
                write_report(out, ctx._audio_report)
                ctx.register_file(name, out, "video", replace_existing=True)
        finally:
            pending.unlink(missing_ok=True)
            from dubforge.pipeline.audio_quality import report_path
            report_path(pending).unlink(missing_ok=True)
        size = out.stat().st_size // 1048576
        ctx.log(f"✅ {'已替换' if replacing else '已生成'} {VARIANT_LABELS[variant]}: {name} ({size} MB)")

    if outputs_scope:
        ctx.log("原片（原声 · 不烧录字幕）即源视频，无需合成，直接使用原文件。")
    ctx.log(f"输出目录: {out_dir}")
