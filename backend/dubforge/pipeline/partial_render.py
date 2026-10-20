"""显式局部成片：范围外保留基片，范围内使用当前干净源重新渲染。"""
import copy
import math
import tempfile
from pathlib import Path

from dubforge.errors import DubForgeError
from dubforge.execution_progress import report_stage
from dubforge.pipeline.render_resources import bounded_ffmpeg_command, ffmpeg_failure
from dubforge.utils.ffmpeg import complex_filter_command


def _exact_frames(ranges, fps):
    """与帧边界求外包，避免秒数的小数误差遗漏末帧。"""
    return [(max(0, math.floor(row['start'] * fps + 1e-7)),
             math.ceil(row['end'] * fps - 1e-7)) for row in ranges]


def _baseline_audio_report(plan):
    """只复用创建计划时按基准文件身份核验且绑定内容哈希的完整测量。"""
    saved = plan.get('baseline_audio_report')
    if (not isinstance(saved, dict) or saved.get('base_sha256') != plan['base_sha256']
            or not isinstance(saved.get('report'), dict)):
        return None
    report = saved['report']
    if (report.get('schema') != 1 or report.get('has_audio') is not True or report.get('preview', False)
            or not isinstance(report.get('settings'), dict)
            or not isinstance(report.get('processing'), dict)
            or not isinstance(report.get('issues'), list)):
        return None
    for key in ('integrated_lufs', 'true_peak_db', 'loudness_range_lu'):
        if key not in report:
            return None
        value = report[key]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            return None
    result = copy.deepcopy(report)
    # 最终登记会绑定新成片文件，不让旧文件身份混进本轮的中间报告。
    result.pop('file', None)
    return result


def review_receipt(ctx, current):
    """局部处理凭证不能替范围外旧画面和旧声音声明重新审查。"""
    plan = ctx.version['partial_plan']
    local = copy.deepcopy(current)
    local['scope'] = 'selected_ranges'
    local['ranges'] = [dict(start=max(row['start'], window['start']), end=min(row['end'], window['end']))
                       for row in current.get('ranges', []) for window in plan['ranges']
                       if max(row['start'], window['start']) < min(row['end'], window['end'])]
    return dict(schema=1, scope='partial', base_sha256=plan['base_sha256'], ranges=copy.deepcopy(plan['ranges']),
        replace_audio=plan['replace_audio'], whole_output_reviewed=False,
        inherited_baseline=copy.deepcopy(plan['base_version'].get('content_review_receipt')),
        current_local=local,
        note='仅所选范围采用当前审查处理；范围外继承基片原记录，未重新审查全片。'
             + ('指定范围声音已重新处理。' if plan['replace_audio'] else '完整音轨沿用基片，未重新处理声音。'))


def render_segment(ctx, base, source, ranges, start_frame, end_frame, info, target):
    """关键帧扩展只是编码范围；只有用户指定的帧使用新字幕与评论。"""
    from dubforge.pipeline.segment_replace import encoding_args
    from dubforge.pipeline.subtitle_style import render_canvas
    from dubforge.pipeline.comment_overlay import prepare_filter

    fps = info['fps_num'] / info['fps_den']
    start, duration = start_frame / fps, (end_frame - start_frame) / fps
    output = Path(target)
    output.parent.mkdir(parents=True, exist_ok=True)
    version = ctx.version
    exact = info.get('requested_frame_ranges')
    if exact is not None:
        exact = [(row['start_frame'], row['end_frame']) for row in exact]
    else:
        exact = _exact_frames(ranges, fps)
    enables = []
    for left, right in exact:
        left, right = max(start_frame, left), min(end_frame, right)
        if right > left:
            enables.append(f'between(n,{left-start_frame},{right-start_frame-1})')
    if not enables:
        raise DubForgeError('局部编码范围没有待替换画面，已保留原成片', code='ASSEMBLY_PARTIAL_RANGE_INVALID')
    # ASS 文件只在本片段目录内，既便于路径转义也可在失败时整体清理。
    with tempfile.TemporaryDirectory(prefix='.tmp_partial_visual_', dir=output.parent) as folder:
        folder = Path(folder)
        clean = ['setpts=PTS-STARTPTS']
        if version['burn_subtitles']:
            # 共享字幕由 version_render 生成；本次 ASS 仍按完整当前稿裁剪到该窗口。
            # 滤镜以工作目录读 ASS，因此只让临时上下文选择同目录的路径。
            from dubforge.pipeline.subtitle_style import prepare_subtitle_lines
            from dubforge.pipeline.version_render import subtitle_lines
            prepared = getattr(ctx, '__dict__', {}).get('_prepared_subtitle_lines')
            track = ctx.cfg('assemble.subtitle_track', 'trans')
            lines = (prepared['lines'] if prepared and prepared['track'] == track
                     else subtitle_lines(ctx, version))
            clean.append(prepare_subtitle_lines(lines, ctx.cfg, render_canvas(ctx, source), folder,
                                                name='partial-subtitle', start=start, end=start + duration))
        comment_filter, _ = prepare_filter(version.get('comment_overlay'), source, folder,
            start=start, duration=duration,
            canvas=render_canvas(ctx, source) if version.get('comment_overlay') else None)
        if comment_filter:
            clean.append(comment_filter)
        sar = str(info.get('sample_aspect_ratio') or '1:1').replace(':', '/')
        graph = [f'[0:v]setpts=PTS-STARTPTS,setsar={sar}[old]',
                 f"[1:v]{','.join(clean)},setsar={sar}[new]",
                 f"[old][new]overlay=shortest=1:eof_action=pass:enable='{'+'.join(enables)}',"
                 f"format={info['pix_fmt']},setsar={sar}[patched]"]
        cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-progress', 'pipe:1', '-nostats',
               '-ss', f'{start:.9f}', '-i', str(base), '-ss', f'{start:.9f}', '-i', str(source),
               '-filter_complex', ';'.join(graph), '-map', '[patched]', '-an',
               '-frames:v', str(end_frame-start_frame),
               *encoding_args(info, crf=int(ctx.cfg('assemble.crf', 18))),
               '-f', 'mpegts', str(output)]
        ctx.check_cancel()
        ctx.check_pause()

        def progress(line):
            if line.startswith('out_time_us='):
                try:
                    seconds = max(0., min(duration, float(line.partition('=')[2]) / 1_000_000))
                except ValueError:
                    return
                report_stage(ctx, 'encoding_partial_window', '重制局部片段画面、字幕与评论',
                             completed=round(seconds, 3), total=round(duration, 3), unit='片段秒',
                             source_start=round(start, 3), source_end=round(start + duration, 3))

        with complex_filter_command(bounded_ffmpeg_command(cmd), folder) as prepared:
            code, _, error = ctx.run_subprocess(prepared, cwd=folder, on_stdout=progress)
        if code:
            raise ffmpeg_failure(error, output_dir=output.parent)
        ctx.check_cancel()


def run(ctx, source, out, total):
    """一次准备候选音轨，多个局部窗口共享它；基准与新版本始终独立。"""
    from dubforge.pipeline.assembly_partial import prepare
    from dubforge.pipeline.segment_replace import replace
    from dubforge.pipeline.step_assemble import _mix_timeline, _mux, _mux_original_sub

    plan = prepare(ctx, ctx.version)
    base = Path(plan['base_path'])
    report_stage(ctx, 'preparing_partial_render', '核对局部替换基片与时间范围',
                 total=len(plan['ranges']), unit='片段')
    with tempfile.TemporaryDirectory(prefix='.tmp_partial_', dir=Path(out).parent) as folder:
        folder = Path(folder)
        audio = None
        if plan['replace_audio']:
            audio = folder / 'candidate.wav'
            if ctx.version['dubbing']:
                voice = _mix_timeline(ctx, ctx.pdir, total,
                    output_name='.tmp_partial_voice.wav', register=False)
                try:
                    _mux(ctx, ctx.pdir, source, voice, audio, total, audio_only=True)
                finally:
                    voice.unlink(missing_ok=True)
            else:
                _mux_original_sub(ctx, source, audio, total, (0., 20.), burn=False, audio_only=True)

        def render(start_frame, end_frame, info, target):
            render_segment(ctx, base, source, plan['ranges'], start_frame, end_frame, info, target)

        receipt = replace(ctx, base, Path(out), plan['ranges'], render, audio_candidate=audio)
    # 报告只描述实际合成物；局部保留旧音轨时不强行应用当前整片响度目标。
    from dubforge.pipeline.audio_quality import measure, settings
    has_audio = bool(plan['base_spec'].get('has_audio'))
    if audio is not None:
        has_audio = True
    reused = _baseline_audio_report(plan) if has_audio and not plan['replace_audio'] else None
    if reused is not None:
        report_stage(ctx, 'reusing_output_audio_measurement', '音轨完整保留，复用基准声音测量', reused=True)
        ctx._audio_report = reused
        ctx._audio_report['processing']['partial_replacement'] = True
        ctx._audio_report['processing']['audio_reused_from_base'] = True
        ctx._audio_report['measurement_provenance'] = dict(kind='baseline_audio_reused',
            base_sha256=plan['base_sha256'], reason='音频压缩包完整复制，测量结果与基片一致；未应用当前声音设置。')
    elif has_audio:
        report_stage(ctx, 'measuring_output_audio', '测量局部替换成片的实际声音')
        measurement = measure(out, ctx=ctx)
        ctx._audio_report = dict(**measurement, has_audio=True, preview=False,
            settings=settings(ctx.cfg), processing={'gain_db': 0., 'peak_limited': False,
            'partial_replacement': True}, target_reached=None, line_report=[])
    else:
        ctx._audio_report = dict(schema=1, has_audio=False, preview=False, issues=[])
    if has_audio:
        issues = [row for row in ctx._audio_report.get('issues', [])
                  if not isinstance(row, dict) or row.get('kind') != 'partial_audio_sources']
        ctx._audio_report['issues'] = [*issues, dict(kind='partial_audio_sources', severity='info', message=(
            '仅指定范围使用新声音，范围外保留基片；当前响度设置不对完整成片重新应用。'
            if plan['replace_audio'] else '完整保留基片音轨，未应用当前混音及响度设置。'))]
    ctx._audio_report['partial_replacement'] = dict(plan_id=plan['id'],
        base_sha256=plan['base_sha256'], replace_audio=plan['replace_audio'],
        ranges=plan['ranges'], measurement_reused=reused is not None, render=receipt)
    ctx.log('局部替换完成：指定范围使用当前内容，其余沿用基片；已生成独立成片版本')
