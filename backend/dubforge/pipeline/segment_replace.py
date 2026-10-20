"""关键帧对齐的成片局部替换；无法证明可安全拼接时保留旧成片。"""
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import threading

from dubforge.errors import DubForgeError
from dubforge.execution_progress import report_stage
from dubforge.pipeline.render_resources import preflight_render, ffmpeg_failure
from dubforge.utils.ffmpeg import complex_filter_command
from dubforge.utils.fs import atomic_replace


_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()


class SegmentReplaceError(DubForgeError):
    code = 'SEGMENT_REPLACE_FAILED'
    suggestion = '请改用完整重制；已有成片保留，字幕与配音无需重新生成。'


def _unsupported(message):
    return SegmentReplaceError(message, code='SEGMENT_REPLACE_UNSUPPORTED')


def _cancel(ctx):
    check = getattr(ctx, 'check_cancel', None)
    if callable(check):
        check()


def _run(ctx, args, *, cwd=None, progress=None):
    _cancel(ctx)
    args = [str(arg) for arg in args]
    try:
        runner = getattr(ctx, 'run_subprocess', None)
        if callable(runner):
            rc, stdout, stderr = runner(args, cwd=cwd, on_stdout=progress)
        else:
            result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=180)
            rc, stdout, stderr = result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SegmentReplaceError('无法检查或处理局部成片媒体', detail=str(exc)) from exc
    _cancel(ctx)
    if rc:
        raise ffmpeg_failure(stderr, output_dir=cwd,
                             fallback_code='SEGMENT_REPLACE_MEDIA_FAILED',
                             message='局部成片媒体处理失败，已有成片保留')
    # FFmpeg会自动修正不连续DTS并返回0；这种产物不允许覆盖成片。
    if any(token in stderr.lower() for token in ('non-monoton', 'invalid dts', 'error while decoding', 'corrupt')):
        raise SegmentReplaceError('局部成片时间戳或编码不兼容，已保留旧成片',
                                  code='SEGMENT_REPLACE_TIMESTAMPS', detail=stderr[-2500:])
    return stdout


def _ffmpeg(ctx, args, *, cwd, progress=None):
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-v', 'warning', '-y',
               '-threads', '2', '-filter_threads', '2', '-filter_complex_threads', '2', *args]
    with complex_filter_command(command, cwd) as prepared:
        return _run(ctx, prepared, cwd=cwd, progress=progress)


def _probe(ctx, path, *args):
    raw = _run(ctx, ['ffprobe', '-v', 'error', *args, '-of', 'json', str(path)])
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise _unsupported('媒体检查结果不完整，暂不能安全局部替换') from exc


def _hex_data(value):
    try:
        return bytes.fromhex(''.join(line.partition(':')[2].split('  ')[0]
                                     for line in str(value or '').splitlines() if ':' in line))
    except ValueError as exc:
        raise _unsupported('媒体编码头无法解析，暂不能安全局部替换') from exc


def _nals(data, length_size=0):
    if length_size:
        result, pos = [], 0
        while pos + length_size <= len(data):
            size = int.from_bytes(data[pos:pos + length_size], 'big')
            pos += length_size
            if size <= 0 or pos + size > len(data):
                raise _unsupported('H.264 数据包结构异常，暂不能安全局部替换')
            result.append(data[pos:pos + size])
            pos += size
        return result
    return [item.rstrip(b'\x00') for item in re.split(b'\x00\x00(?:\x00)?\x01', data) if item]


def _parameter_hash(data):
    if data and data[0] == 1:
        # MP4的AVCC头与TS的AnnexB头表示不同；比较真实SPS/PPS，不能比较封装字节。
        try:
            pos, items = 6, []
            for _ in range(data[5] & 31):
                size = int.from_bytes(data[pos:pos + 2], 'big')
                pos += 2
                items.append(data[pos:pos + size])
                pos += size
            count, pos = data[pos], pos + 1
            for _ in range(count):
                size = int.from_bytes(data[pos:pos + 2], 'big')
                pos += 2
                items.append(data[pos:pos + size])
                pos += size
        except (IndexError, ValueError) as exc:
            raise _unsupported('H.264 参数头不完整，暂不能安全局部替换') from exc
    else:
        items = _nals(data)
    items = sorted(set(item for item in items if item and item[0] & 31 in {7, 8}))
    if not items or {item[0] & 31 for item in items} != {7, 8}:
        raise _unsupported('未找到完整的H.264画面参数，暂不能安全局部替换')
    return hashlib.sha256(b''.join(len(item).to_bytes(4, 'big') + item for item in items)).hexdigest()


def _inspect(path, *, ctx=None, segment=False, require_encoder=True):
    path = Path(path)
    if not path.is_file():
        raise SegmentReplaceError('作为基础的成片不存在', code='SEGMENT_REPLACE_BASE_MISSING')
    meta = _probe(ctx, path, '-show_streams', '-show_format', '-show_data')
    streams = meta.get('streams', [])
    videos = [stream for stream in streams if stream.get('codec_type') == 'video']
    if len(videos) != 1:
        raise _unsupported('局部替换仅支持包含一条画面轨道的成片')
    video = videos[0]
    if video.get('codec_name') != 'h264' or video.get('pix_fmt') != 'yuv420p':
        raise _unsupported('局部替换目前仅支持8位H.264 yuv420p成片，请使用完整重制')
    if video.get('field_order', 'progressive') not in {'progressive', 'unknown'}:
        raise _unsupported('隔行视频暂不支持局部替换，请使用完整重制')
    try:
        fps = Fraction(video['avg_frame_rate'])
        rate = Fraction(video['r_frame_rate'])
        tick = Fraction(video['time_base'])
        if fps <= 0 or fps != rate:
            raise ValueError('不是固定帧率')
    except (ValueError, KeyError, ZeroDivisionError) as exc:
        raise _unsupported('可变帧率或未知帧率的成片暂不支持局部替换') from exc
    payload = _probe(ctx, path, '-select_streams', 'v:0', '-read_intervals', '%+#1',
                     '-show_packets', '-show_data', '-show_entries', 'packet=data')
    packets = payload.get('packets', [])
    raw = _hex_data(packets[0].get('data')) if packets else b''
    length_size = int(video.get('nal_length_size') or 0) if video.get('is_avc') == 'true' else 0
    if not any(item and item[0] & 31 == 5 for item in _nals(raw, length_size)):
        raise _unsupported('成片开头不是独立IDR画面，暂不能安全局部替换')
    sei = re.search(rb'x264 - core [^\x00]*?options: ([^\x00]+)', raw)
    options = dict(re.findall(r'([\w_]+)=([^ ]+)', sei[1].decode('ascii', 'replace'))) if sei else {}
    try:
        bframes = int(options['bframes']) if options else None
    except (ValueError, TypeError):
        bframes = None
    if require_encoder and (bframes is None or options.get('interlaced') != '0'
                            or options.get('intra_refresh') != '0'
                            or bframes > 0 and options.get('open_gop') != '0'):
        raise _unsupported('无法确认成片使用封闭GOP的x264编码，请使用完整重制')
    packet_text = _run(ctx, ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                            '-show_packets', '-show_entries', 'packet=pts,dts,duration,flags',
                            '-of', 'compact=p=0:nk=0', str(path)])
    pts_indices, key_packets, previous_dts, origin = set(), [], None, None
    for index, line in enumerate(line for line in packet_text.splitlines() if line.startswith('pts=')):
        if index % 1024 == 0:
            _cancel(ctx)
        row = dict(part.split('=', 1) for part in line.split('|') if '=' in part)
        try:
            pts, dts, duration = int(row['pts']), int(row['dts']), int(row['duration'])
        except (KeyError, ValueError) as exc:
            raise _unsupported('成片含不完整的时间戳，暂不能安全局部替换') from exc
        if previous_dts is not None and dts <= previous_dts:
            raise _unsupported('成片解码时间戳不连续，请使用完整重制')
        previous_dts = dts
        if origin is None:
            origin = pts
        frame_float = float((pts - origin) * tick * fps)
        frame_index = round(frame_float)
        # MPEG-TS使用90kHz时钟，23.976fps会交替舍入，不可误判为VFR。
        if (abs(float(duration * tick * fps) - 1) > 0.001 or frame_index in pts_indices
                or frame_index < 0 or abs(frame_float - frame_index) > 0.001):
            raise _unsupported('成片含可变帧率或重复时间戳，请使用完整重制')
        pts_indices.add(frame_index)
        if 'K' in row.get('flags', ''):
            key_packets.append((index, pts))
    if not pts_indices:
        raise _unsupported('成片没有可读取的画面帧')
    count = len(pts_indices)
    if not segment and abs(float(origin * tick)) > float(1 / fps) / 10:
        raise _unsupported('成片时间轴不从零开始，请使用完整重制')
    if min(pts_indices) != 0 or max(pts_indices) != count - 1:
        raise _unsupported('成片画面时间戳存在间隙或可变帧率，请使用完整重制')
    keys = []
    for packet_index, pts in key_packets:
        frame = round(float((pts - origin) * tick * fps))
        if frame != packet_index:
            raise _unsupported('成片GOP边界跨越重排画面，暂不能安全局部替换')
        keys.append(frame)
    if not keys or keys[0] != 0:
        raise _unsupported('成片缺少独立的开头关键帧')
    duration = float(Fraction(count, 1) / fps)
    audios = [stream for stream in streams if stream.get('codec_type') == 'audio']
    info = dict(path=str(path.resolve()), fps=float(fps), fps_num=fps.numerator, fps_den=fps.denominator,
                frame_count=count, duration=duration, width=int(video['width']), height=int(video['height']),
                pix_fmt=video['pix_fmt'], profile=video.get('profile'), level=video.get('level'),
                bframes=bframes, has_b_frames=int(video.get('has_b_frames') or 0),
                keyframe_frames=keys, keyframes=[dict(frame=n, time=float(Fraction(n, 1) / fps)) for n in keys],
                x264_options=options, parameter_hash=_parameter_hash(_hex_data(video.get('extradata'))),
                sample_aspect_ratio=video.get('sample_aspect_ratio', '1:1'), has_audio=bool(audios),
                audio_codec=audios[0].get('codec_name') if audios else None,
                audio_duration=float(audios[0].get('duration') or 0) if audios else None,
                pts_start=float(origin * tick), packet_count=count,
                size_bytes=path.stat().st_size, mtime_ns=path.stat().st_mtime_ns)
    for field in ('color_range', 'color_space', 'color_transfer', 'color_primaries'):
        info[field] = video.get(field)
    return info


def inspect_base(path, *, ctx=None):
    """只探测编码头和压缩包，不解码整片；未知编码安全回退全量。"""
    _cancel(ctx)
    path = Path(path).resolve()
    try:
        stat = path.stat()
    except OSError:
        return _inspect(path, ctx=ctx)
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return deepcopy(cached)
    result = _inspect(path, ctx=ctx)
    with _CACHE_LOCK:
        _CACHE[key] = deepcopy(result)
        while len(_CACHE) > 8:
            _CACHE.popitem(last=False)
    return result


def plan_windows(info, ranges):
    """把任意秒范围合并，量化到帧，再向外扩到独立关键帧。"""
    if not isinstance(ranges, (list, tuple)) or not ranges:
        raise SegmentReplaceError('请至少选择一个需要替换的时间范围', code='SEGMENT_REPLACE_RANGES')
    duration, fps, count = info['duration'], info['fps'], info['frame_count']
    normalized = []
    for item in ranges:
        try:
            start, end = (item['start'], item['end']) if isinstance(item, dict) else item
            if type(start) not in {int, float} or type(end) not in {int, float}:
                raise ValueError()
            if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or start >= duration
                    or end <= start or end > duration + 0.001):
                raise ValueError()
        except (ValueError, TypeError, KeyError) as exc:
            raise SegmentReplaceError('替换范围必须是成片内有效的起止秒数', code='SEGMENT_REPLACE_RANGES') from exc
        normalized.append([float(start), min(float(end), duration)])
    def merge(items):
        result = []
        for start, end in sorted(items):
            if result and start <= result[-1][1] + 1e-8:
                result[-1][1] = max(end, result[-1][1])
            else:
                result.append([start, end])
        return result
    normalized = merge(normalized)
    frames = []
    for start, end in normalized:
        first = max(0, min(count - 1, math.floor(start * fps + 1e-7)))
        last = min(count, max(first + 1, math.ceil(end * fps - 1e-7)))
        frames.append([first, last])
    frames = merge(frames)
    keys = sorted(set([*info['keyframe_frames'], count]))
    expanded = merge([[keys[bisect_right(keys, start) - 1], keys[bisect_left(keys, end)]]
                      for start, end in frames])
    def window(span):
        start, end = span
        return dict(start_frame=start, end_frame=end, start=start / fps, end=end / fps)
    windows = [window(span) for span in expanded]
    return dict(requested_ranges=[dict(start=start, end=end) for start, end in normalized],
                requested_frame_ranges=[window(span) for span in frames], windows=windows,
                expanded_ranges=[dict(start=w['start'], end=w['end']) for w in windows],
                encode_seconds=sum(w['end'] - w['start'] for w in windows), duration=duration,
                encoded_frames=sum(w['end_frame'] - w['start_frame'] for w in windows),
                reused_frames=count - sum(w['end_frame'] - w['start_frame'] for w in windows))


def encoding_args(info, *, crf=18):
    """重用编码头相关选项；画面滤镜仍由调用方提供。"""
    options = info['x264_options']
    copied = {key: options[key] for key in ('ref', 'bframes', 'b_pyramid', 'cabac', '8x8dct', 'weightp',
              'weightb', 'keyint', 'keyint_min', 'scenecut') if key in options}
    # SEI中的chroma_qp_offset是psy调整后的结果，再传回会重复减2；
    # 本工具生成的标准x264成片沿用默认值，自定义非标准头由后续兼容检查拒绝。
    copied.update({'open-gop': '0', 'repeat-headers': '1'})
    if options.get('deblock'):
        values = options['deblock'].split(':')
        copied['deblock'] = ','.join(values[1:]) if values[0] == '1' else '0,0'
        if values[0] == '0':
            copied['no-deblock'] = '1'
    sar = info.get('sample_aspect_ratio') or '1:1'
    if sar not in {'N/A', '0:1'}:
        copied['sar'] = sar.replace(':', '/')
    args = ['-c:v', 'libx264', '-threads', '2', '-preset', 'medium', '-crf', str(crf),
            '-pix_fmt', info['pix_fmt'], '-r', f"{info['fps_num']}/{info['fps_den']}",
            '-fps_mode', 'cfr', '-x264-params', ':'.join(f'{key}={value}' for key, value in copied.items())]
    if info.get('profile') in {'High', 'Main', 'Constrained Baseline', 'Baseline'}:
        args += ['-profile:v', {'Constrained Baseline': 'baseline', 'Baseline': 'baseline'}.get(info['profile'], info['profile'].lower())]
    if info.get('level'):
        args += ['-level:v', str(info['level'] / 10)]
    for key, option in (('color_range', '-color_range'), ('color_space', '-colorspace'),
                        ('color_transfer', '-color_trc'), ('color_primaries', '-color_primaries')):
        if info.get(key) and info[key] not in {'unknown', 'unspecified', 'reserved'}:
            args += [option, info[key]]
    return args


def _compatible(base, candidate, frames):
    for key in ('width', 'height', 'pix_fmt', 'fps_num', 'fps_den', 'profile', 'level',
                'has_b_frames', 'parameter_hash'):
        if candidate.get(key) != base.get(key):
            raise SegmentReplaceError('替换片段与原成片的画面编码不一致，已保留旧成片',
                code='SEGMENT_REPLACE_INCOMPATIBLE', detail=dict(field=key, base=base.get(key), segment=candidate.get(key)))
    if candidate['frame_count'] != frames:
        raise SegmentReplaceError('替换片段帧数与预定范围不一致，已保留旧成片',
                                  code='SEGMENT_REPLACE_FRAME_COUNT')


def _audio_filter(ranges, duration):
    # aeval按采样点选择；范围内侧5ms交叉淡化，避免波形相位不同产生啪声。
    # 不触碰范围外，不调整时长；很短范围则把淡化缩到一半长度。
    active = '+'.join(f'clip(min((t-{r["start"]:.9f})/{max(1 / 48000, min(.005, (r["end"]-r["start"])/2)):.9f},'
                      f'({r["end"]:.9f}-t)/{max(1 / 48000, min(.005, (r["end"]-r["start"])/2)):.9f}),0,1)' for r in ranges)
    return ('[1:a:0]aformat=sample_fmts=dbl:sample_rates=48000:channel_layouts=stereo,'
            f'apad,atrim=duration={duration:.9f},asetpts=PTS-STARTPTS[old];'
            '[2:a:0]aformat=sample_fmts=dbl:sample_rates=48000:channel_layouts=stereo,'
            f'atrim=duration={duration:.9f},asetpts=PTS-STARTPTS[new];'
            f"[old][new]amerge=inputs=2,aeval=exprs='val(0)*(1-({active}))+val(2)*({active})|"
            f"val(1)*(1-({active}))+val(3)*({active})':channel_layout=stereo,"
            'aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a]')


def replace(ctx, base, out, ranges, render_segment, *, audio_candidate=None):
    """仅编码关键帧外扩窗口；成功且完整校验后才原子更新out。"""
    base, out = Path(base).resolve(), Path(out).resolve()
    _cancel(ctx)
    report_stage(ctx, 'checking_segment_base', '检查局部重制基础成片')
    info = inspect_base(base, ctx=ctx)
    plan = plan_windows(info, ranges)
    out.parent.mkdir(parents=True, exist_ok=True)
    preflight_render(ctx, out.parent, required_bytes=max(8 * 1024 ** 2, info['size_bytes'] * 3))
    callback_info = {**info, 'requested_ranges': plan['requested_ranges'],
                     'requested_frame_ranges': plan['requested_frame_ranges'], 'expanded_windows': plan['windows']}
    with tempfile.TemporaryDirectory(prefix='.dubforge-segments-', dir=out.parent) as folder:
        work = Path(folder)
        spans, cursor = [], 0
        for window in plan['windows']:
            if cursor < window['start_frame']:
                spans.append((cursor, window['start_frame'], False))
            spans.append((window['start_frame'], window['end_frame'], True))
            cursor = window['end_frame']
        if cursor < info['frame_count']:
            spans.append((cursor, info['frame_count'], False))
        files, durations = [], []
        for index, (start, end, dirty) in enumerate(spans):
            _cancel(ctx)
            report_stage(ctx, 'rendering_segments' if dirty else 'reusing_segments',
                         '重制修改片段' if dirty else '复用未修改画面',
                         detail=f'{start / info["fps"]:.3f}–{end / info["fps"]:.3f} 秒',
                         completed=index, total=len(spans), unit='片段')
            target = work / f'part-{index:04d}.ts'
            if dirty:
                render_segment(start, end, callback_info, target)
                segment = _inspect(target, ctx=ctx, segment=True, require_encoder=False)
                _compatible(info, segment, end - start)
            else:
                _ffmpeg(ctx, ['-ss', f'{start / info["fps"]:.9f}', '-i', str(base),
                    '-map', '0:v:0', '-an', '-frames:v', str(end - start), '-c:v', 'copy',
                    '-bsf:v', 'h264_mp4toannexb', '-muxdelay', '0', '-f', 'mpegts', str(target)], cwd=work)
            files.append(target)
            durations.append((end - start) / info['fps'])
        listing = work / 'concat.txt'
        listing.write_text(''.join(f"file '{p.name}'\nduration {d:.12f}\n" for p, d in zip(files, durations)), encoding='utf-8')
        video = work / 'video.mp4'
        report_stage(ctx, 'joining_segments', '拼接新旧画面片段')
        _ffmpeg(ctx, ['-f', 'concat', '-safe', '0', '-i', str(listing), '-map', '0:v:0',
            '-an', '-c:v', 'copy', '-video_track_timescale', str(info['fps_num']), '-movflags', '+faststart', str(video)], cwd=work)
        # TS总和、拼接视频、最终文件均可能接近一份基片；尽早删掉上一阶段中间件。
        for target in files:
            target.unlink(missing_ok=True)
        listing.unlink(missing_ok=True)
        temporary = work / 'final.tmp.mp4'
        cmd = ['-i', str(video), '-i', str(base)]
        if audio_candidate is not None:
            candidate = Path(audio_candidate)
            audio_meta = _probe(ctx, candidate, '-show_streams', '-show_format')
            audio = next((s for s in audio_meta.get('streams', []) if s.get('codec_type') == 'audio'), None)
            try:
                seconds = float((audio or {}).get('duration') or audio_meta.get('format', {}).get('duration') or 0)
            except ValueError:
                seconds = 0
            if not audio or not str(audio.get('codec_name', '')).startswith('pcm_') or seconds < info['duration'] - 1 / 48000:
                raise SegmentReplaceError('新音轨必须是覆盖完整成片时间轴的PCM WAV', code='SEGMENT_REPLACE_AUDIO')
            if not info['has_audio']:
                raise _unsupported('无声基础成片不能局部替换为有声音轨，请使用完整重制')
            cmd += ['-i', str(candidate), '-filter_complex', _audio_filter(plan['requested_ranges'], info['duration']),
                    '-map', '0:v:0', '-map', '[a]', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                    '-threads:a', '2', '-ac', '2', '-t', f'{info["duration"]:.9f}']
        else:
            cmd += ['-map', '0:v:0', '-map', '1:a:0?', '-c', 'copy']
        report_stage(ctx, 'joining_segment_audio', '替换片段声音并封装成片' if audio_candidate is not None else '保留原音轨并封装成片')
        cmd += ['-map_metadata', '1', '-map_chapters', '1', '-movflags', '+faststart', str(temporary)]
        _ffmpeg(ctx, cmd, cwd=work)
        video.unlink(missing_ok=True)
        report_stage(ctx, 'validating_segment_output', '核对局部重制帧数与时间轴')
        final = _inspect(temporary, ctx=ctx, require_encoder=False)
        _compatible(info, final, info['frame_count'])
        if abs(final['duration'] - info['duration']) > 1e-5 or final['has_audio'] != info['has_audio']:
            raise SegmentReplaceError('局部重制后的音视频时长或轨道不一致，已保留旧成片', code='SEGMENT_REPLACE_DURATION')
        if final['has_audio'] and (not final['audio_duration'] or abs(final['audio_duration'] - info['duration']) > 0.1):
            raise SegmentReplaceError('局部重制后的声音时间轴不完整，已保留旧成片', code='SEGMENT_REPLACE_AUDIO')
        # 真实长片只检查压缩包/参数/时间轴；逐帧像素hash与完整解码由隔离媒体测试覆盖。
        _cancel(ctx)
        if base.stat().st_size != info['size_bytes'] or base.stat().st_mtime_ns != info['mtime_ns']:
            raise SegmentReplaceError('基础成片在处理期间发生变化，已取消替换', code='SEGMENT_REPLACE_BASE_CHANGED')
        atomic_replace(temporary, out)
    report_stage(ctx, 'segment_output_ready', '局部重制完成', state='completed',
                 completed=len(spans), total=len(spans), unit='片段')
    return {**plan, 'base': str(base), 'output': str(out), 'audio_mode': 'replace_ranges' if audio_candidate is not None else 'copy',
            'frame_count': info['frame_count'], 'validated': True}
