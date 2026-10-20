"""精确删除成片片段：只重编码接缝，完整封闭 GOP 直接复制。"""
from bisect import bisect_left, bisect_right
import copy
import math
from pathlib import Path
import tempfile

from dubforge.errors import DubForgeError
from dubforge.execution_progress import report_stage
from dubforge.pipeline import segment_replace as sr
from dubforge.pipeline.render_resources import preflight_render
from dubforge.utils.fs import atomic_replace, atomic_write_bytes

VERSION = 1


def _full(reason, code, duration, *, audio=True):
    return dict(method='full_encode', copied_seconds=0., encoded_seconds=duration,
                output_duration=duration, audio_mode='encode_once' if audio else 'none',
                fallback_reason=reason, fallback_code=code)


def summary(value):
    """公开实际工作量，不把可复制 GOP 误称成任意格式无损。"""
    return {key: copy.deepcopy(value[key]) for key in ('method', 'copied_seconds', 'encoded_seconds',
        'output_duration', 'output_frames', 'audio_mode', 'fallback_reason', 'fallback_code',
        'quantization_seconds', 'boundary_quantization_seconds', 'source_tail_gap_seconds', 'quantized_ranges') if key in value}


def analyze(path, keep_ranges, *, ctx=None):
    """保留区间向内取整到帧，等价于把删除区间向外取整。"""
    seconds = sum(float(row['end']) - float(row['start']) for row in keep_ranges)
    try:
        info = sr.inspect_base(path, ctx=ctx)
    except sr.SegmentReplaceError as exc:
        return _full(exc.message, exc.code, seconds)
    fps, count = info['fps'], info['frame_count']
    keys = sorted(set(info['keyframe_frames'] + [count]))
    kept, spans = [], []
    previous = 0.
    for index, row in enumerate(keep_ranges):
        start, end = row['start'], row['end']
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or start < previous - 1e-6 or end <= start or end > info['duration'] + .08):
            return _full('保留区间无法安全映射到成片帧时间轴。', 'SEGMENT_CUT_RANGE', seconds, audio=info['has_audio'])
        previous = end
        left = max(0, min(count, math.ceil(start * fps - 1e-7)))
        right = max(0, min(count, math.floor(end * fps + 1e-7)))
        # 很短的保留片段若不足一整帧，删除向外取整会自然消除它。
        if right <= left:
            continue
        kept.append(dict(index=index, start_frame=left, end_frame=right, start=left / fps, end=right / fps))
        next_key = keys[min(len(keys) - 1, bisect_left(keys, left))]
        last_key = keys[max(0, bisect_right(keys, right) - 1)]
        middle_start, middle_end = min(right, next_key), max(left, last_key)
        # 极短接缝不能提供与原片一致的 B 帧解码预滚。优先从同一个
        # 保留区间的复制主体借相邻完整 GOP，边界仍落在独立 IDR 上；
        # 不跨越删除区间，也不因一两帧接缝直接重编码整部视频。
        if middle_end > middle_start:
            minimum = max(3, info.get('has_b_frames', 0) + 1)
            if left < middle_start and middle_start - left < minimum:
                position = min(len(keys) - 1, bisect_right(keys, middle_start))
                middle_start = min(right, keys[position])
            if middle_end < right and right - middle_end < minimum:
                position = max(0, bisect_left(keys, middle_end) - 1)
                middle_end = max(left, keys[position])
        if middle_end <= middle_start:
            spans.append(dict(start_frame=left, end_frame=right, encode=True))
        else:
            if left < middle_start:
                spans.append(dict(start_frame=left, end_frame=middle_start, encode=True))
            spans.append(dict(start_frame=middle_start, end_frame=middle_end, encode=False))
            if middle_end < right:
                spans.append(dict(start_frame=middle_end, end_frame=right, encode=True))
    if not kept:
        raise sr.SegmentReplaceError('删除范围按帧对齐后没有可保留画面，请至少保留一帧。', code='SEGMENT_CUT_EMPTY')
    encoded = sum(row['end_frame'] - row['start_frame'] for row in spans if row['encode'])
    copied = sum(row['end_frame'] - row['start_frame'] for row in spans if not row['encode'])
    duration = (encoded + copied) / fps
    too_short = any(row['encode'] and row['end_frame'] - row['start_frame'] < max(3, info.get('has_b_frames', 0) + 1)
                    for row in spans)
    if too_short:
        result = _full('保留的极短片段没有可借用的完整 GOP，无法维持可验证的解码与时间戳，需完整编码保证不丢帧。',
                       'SEGMENT_CUT_SHORT_REORDER_WINDOW', duration, audio=info['has_audio'])
    elif not copied:
        result = _full('保留片段均落在剪辑接缝附近，没有可直接复制的完整 GOP。',
                       'SEGMENT_CUT_NO_REUSABLE_GOP', duration, audio=info['has_audio'])
    else:
        result = dict(method='smart_cut', copied_seconds=copied / fps, encoded_seconds=encoded / fps,
            output_duration=duration, audio_mode='encode_once' if info['has_audio'] else 'none',
            fallback_reason='', fallback_code='')
    tail_gap = sum(max(0., row['end'] - max(row['start'], info['duration'])) for row in keep_ranges)
    quantization = max(0., seconds - duration)
    result.update(version=VERSION, output_frames=encoded + copied, info=info, keep_ranges=kept, spans=spans,
                  quantization_seconds=round(quantization, 9), source_tail_gap_seconds=round(tail_gap, 9),
                  boundary_quantization_seconds=round(max(0., quantization - tail_gap), 9),
                  quantized_ranges=[dict(start=row['start'], end=row['end']) for row in kept])
    return result


def _encode(ctx, base, target, span, info, crf, work):
    start, end = span['start_frame'], span['end_frame']
    sr._ffmpeg(ctx, ['-ss', f'{start / info["fps"]:.12f}', '-i', str(base), '-map', '0:v:0', '-an',
        '-frames:v', str(end - start), *sr.encoding_args(info, crf=crf), '-muxdelay', '0',
        '-f', 'mpegts', str(target)], cwd=work)
    checked = sr._inspect(target, ctx=ctx, segment=True, require_encoder=False)
    sr._compatible(info, checked, end - start)


def _audio_seek_offset(ctx, base, kept):
    """音频只从首个保留点前一秒解码；偏移固定在 48kHz 采样格点。"""
    first = min(row['start'] for row in kept)
    offset = max(0, math.floor(first * 48000) - 48000)
    if not offset:
        return 0
    # 输入 -ss 默认相对容器起点定位。非零/未知起点先沿用完整解码，
    # 不用 seek_timestamp/copyts 猜测 edit list 与负音频时间戳的语义。
    meta = sr._probe(ctx, base, '-show_entries', 'format=start_time:stream=codec_type,sample_rate')
    try:
        origin = float(meta['format']['start_time'])
        audio = next(row for row in meta.get('streams', []) if row.get('codec_type') == 'audio')
        rate = int(audio['sample_rate'])
    except (KeyError, TypeError, ValueError, OverflowError, StopIteration):
        return 0
    if not math.isfinite(origin) or abs(origin) > 1e-9 or rate <= 0:
        return 0
    # 同时对齐源/目标采样格点，避免 44.1→48kHz 重采样器在 seek 后
    # 改变分数相位；48kHz 为逐样本，44.1kHz 为每 160 个目标样本。
    grid = 48000 // math.gcd(rate, 48000)
    return offset // grid * grid


def _audio_graph(kept, offset_samples=0):
    # 音轨可能比画面晚开始或提前结束：先按视频零点补齐静音，再按
    # 同一采样时轴裁剪，不能分别 setpts 把本应保留的空白压缩掉。
    graph = [('[1:a:0]aresample=48000:async=1:first_pts=0,'
              'aformat=sample_fmts=fltp:channel_layouts=stereo,apad,'
              f'asplit={len(kept)}' + ''.join(f'[source{index}]' for index in range(len(kept))))]
    labels = []
    for index, row in enumerate(kept):
        # 先对绝对时点取样，再减整数偏移，与原路径选中完全相同的样本；
        # 先减浮点秒再 round 可能在非整数切点多/少一个采样。
        graph.append(f'[source{index}]atrim=start_sample={round(row["start"] * 48000) - offset_samples}:'
                     f'end_sample={round(row["end"] * 48000) - offset_samples},asetpts=PTS-STARTPTS[a{index}]')
        labels.append(f'[a{index}]')
    graph.append(''.join(labels) + f'concat=n={len(kept)}:v=0:a=1[aout]')
    return ';'.join(graph)


def render(ctx, base, out, plan, *, crf=18):
    """只有帧数、时间戳及编码参数都通过校验才替换目标文件。"""
    base, out = Path(base).resolve(), Path(out).resolve()
    sr._cancel(ctx)
    info = sr.inspect_base(base, ctx=ctx)
    if plan.get('method') != 'smart_cut' or any(info[key] != plan['info'][key] for key in
            ('fps_num', 'fps_den', 'frame_count', 'parameter_hash', 'width', 'height')):
        raise sr.SegmentReplaceError('剪辑基片编码或帧时间轴与检查结果不一致。', code='SEGMENT_CUT_BASE_CHANGED')
    out.parent.mkdir(parents=True, exist_ok=True)
    preflight_render(ctx, out.parent, required_bytes=max(8 * 1024 ** 2, info['size_bytes'] * 3))
    with tempfile.TemporaryDirectory(prefix='.dubforge-smart-cut-', dir=out.parent) as folder:
        work, files, durations = Path(folder), [], []
        total = len(plan['spans'])
        for index, span in enumerate(plan['spans']):
            sr._cancel(ctx)
            getattr(ctx, 'check_pause', lambda: None)()
            start, end = span['start_frame'], span['end_frame']
            report_stage(ctx, 'encoding_cut_seams' if span['encode'] else 'copying_complete_gops',
                '重编码剪辑接缝' if span['encode'] else '直接复用完整 GOP 画面', completed=index,
                total=total, unit='片段', detail=f'{start / info["fps"]:.3f}–{end / info["fps"]:.3f} 秒')
            target = work / f'part-{index:04d}.ts'
            if span['encode']:
                _encode(ctx, base, target, span, info, crf, work)
            else:
                sr._ffmpeg(ctx, ['-ss', f'{start / info["fps"]:.12f}', '-i', str(base),
                    '-map', '0:v:0', '-an', '-frames:v', str(end - start), '-c:v', 'copy',
                    '-bsf:v', 'h264_mp4toannexb', '-muxdelay', '0', '-f', 'mpegts', str(target)], cwd=work)
            files.append(target)
            durations.append((end - start) / info['fps'])
        listing, video = work / 'concat.txt', work / 'video.mp4'
        atomic_write_bytes(listing, ''.join(f"file '{p.name}'\nduration {d:.12f}\n"
            for p, d in zip(files, durations)).encode())
        report_stage(ctx, 'joining_cut_video', '拼接复用画面与剪辑接缝')
        getattr(ctx, 'check_pause', lambda: None)()
        sr._ffmpeg(ctx, ['-f', 'concat', '-safe', '0', '-i', str(listing), '-map', '0:v:0', '-an',
            '-c:v', 'copy', '-video_track_timescale', str(info['fps_num']), '-movflags', '+faststart', str(video)], cwd=work)
        for path in files:
            path.unlink(missing_ok=True)
        listing.unlink(missing_ok=True)
        temporary = work / 'final.tmp.mp4'
        command = ['-i', str(video)]
        audio_offset_samples = 0
        if info['has_audio']:
            audio_offset_samples = _audio_seek_offset(ctx, base, plan['keep_ranges'])
            audio_seek = ['-ss', f'{audio_offset_samples / 48000:.12f}'] if audio_offset_samples else []
            command += [*audio_seek, '-i', str(base), '-filter_complex', _audio_graph(plan['keep_ranges'], audio_offset_samples),
                        '-map', '0:v:0', '-map', '[aout]', '-c:v', 'copy', '-c:a', 'aac',
                        '-b:a', '192k', '-threads:a', '2', '-ac', '2']
        else:
            command += ['-map', '0:v:0', '-c:v', 'copy', '-an']
        report_stage(ctx, 'cutting_audio_once', '精确裁剪音轨并一次编码 AAC' if info['has_audio'] else '封装无声剪辑成片')
        getattr(ctx, 'check_pause', lambda: None)()
        command += ['-t', f'{plan["output_duration"]:.12f}', '-map_chapters', '-1', '-movflags', '+faststart', str(temporary)]
        sr._ffmpeg(ctx, command, cwd=work)
        video.unlink(missing_ok=True)
        report_stage(ctx, 'validating_smart_cut', '核对剪辑帧数、时间戳、编码头与音视频时长')
        final = sr._inspect(temporary, ctx=ctx, require_encoder=False)
        sr._compatible(info, final, plan['output_frames'])
        if (abs(final['duration'] - plan['output_duration']) > 1e-6 or final['has_audio'] != info['has_audio']
                or final['has_audio'] and abs((final['audio_duration'] or 0) - plan['output_duration']) > .1):
            raise sr.SegmentReplaceError('剪辑输出的帧数或音视频时长不一致，已保留旧成片。', code='SEGMENT_CUT_DURATION')
        if base.stat().st_size != info['size_bytes'] or base.stat().st_mtime_ns != info['mtime_ns']:
            raise sr.SegmentReplaceError('剪辑过程中基片发生变化，已取消本次输出。', code='SEGMENT_CUT_BASE_CHANGED')
        sr._cancel(ctx)
        atomic_replace(temporary, out)
    result = summary(plan)
    result.update(validated=True, copied_frames=round(plan['copied_seconds'] * info['fps']),
                  encoded_frames=round(plan['encoded_seconds'] * info['fps']),
                  audio_seek_offset_seconds=audio_offset_samples / 48000)
    return result
