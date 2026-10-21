"""完整混音统一响度：两遍测量、无损主音轨和可恢复缓存。

先处理完整成片音轨，试片只截取同一主音轨，避免短片各自归一造成试听误导。
"""
import copy
import hashlib
import json
import math
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from dubforge.errors import DubForgeError
from dubforge.execution_progress import report_stage
from dubforge.utils.ffmpeg import complex_filter_command
from dubforge.utils.fs import atomic_replace, atomic_write_bytes


ALGORITHM = 'full-mix-loudnorm-v2-48k-stereo-lra50'
_HEADROOM = .3


def _check(ctx):
    check = getattr(ctx, 'check_cancel', None)
    if callable(check):
        check()
    cancel = getattr(ctx, '_audio_cancel', None)
    if cancel is not None and (cancel.is_set() if hasattr(cancel, 'is_set') else cancel()):
        raise DubForgeError('声音统一已取消，已有成片保留', code='AUDIO_MASTER_CANCELLED')


def _hash_file(path, ctx):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while data := stream.read(1024 * 1024):
            _check(ctx)
            digest.update(data)
    return digest.hexdigest()


def _identity(path, ctx):
    path = Path(path).resolve()
    stat = path.stat()
    # 铺轨文件每次运行可能重写或随机命名；使用内容身份，字幕/评论不参与缓存。
    generated = (path.suffix.lower() in {'.wav', '.flac'}
                 and (path.name.startswith(('.tmp_', 'preview_dub_')) or path.name == 'dub_full.wav'))
    if generated:
        return dict(content_sha256=_hash_file(path, ctx), size=stat.st_size)
    return dict(path=str(path), size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def _signature(ctx, cmd, filters, label, cut_plan, duration, options, *, algorithm=ALGORITHM):
    canonical, inputs = [], []
    args = [str(value) for value in cmd]
    index = 1
    while index < len(args):
        arg = args[index]
        if arg in {'-hide_banner', '-nostats', '-y', '-n'}:
            index += 1
            continue
        if arg in {'-progress', '-loglevel', '-v'} and index + 1 < len(args):
            index += 2
            continue
        if arg == '-i' and index + 1 < len(args):
            identity = _identity(args[index + 1], ctx)
            inputs.append(identity)
            canonical.extend(['-i', {'input': len(inputs) - 1, 'identity': identity}])
            index += 2
            continue
        canonical.append(arg)
        index += 1
    if not inputs:
        raise DubForgeError('完整混音没有可验证的输入文件', code='AUDIO_MASTER_INPUT_INVALID')
    value = dict(algorithm=algorithm, command=canonical, filters=filters, label=label,
                 cut_plan=cut_plan, duration=duration, options=options)
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


@contextmanager
def _cache_lock(path, ctx):
    """操作系统文件锁支持线程与多进程；进程退出自动释放，不留下永久忙碌标志。"""
    with path.open('a+b') as handle:
        # Windows 可锁住 EOF 之外的字节；预写占位会与另一持锁者竞争，空文件也直接加锁。
        while True:
            _check(ctx)
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                time.sleep(.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)


def _run(ctx, cmd, cwd):
    _check(ctx)
    command = list(cmd)
    if '-filter_complex_threads' not in command:
        command[1:1] = ['-filter_complex_threads', '1']
    from dubforge.pipeline.render_resources import bounded_ffmpeg_command, ffmpeg_failure
    command = bounded_ffmpeg_command(command)
    with complex_filter_command(command, cwd) as command:
        runner = getattr(ctx, 'run_subprocess', None)
        if callable(runner):
            code, out, err = runner(command)
        else:
            # 无步骤上下文时仍轮询取消，不用长阻塞 subprocess.run。
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding='utf-8', errors='replace')
            try:
                while True:
                    _check(ctx)
                    try:
                        out, err = process.communicate(timeout=.2)
                        code = process.returncode
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
    _check(ctx)
    if code:
        raise ffmpeg_failure(err, output_dir=cwd, fallback_code='AUDIO_MASTER_PROCESS_FAILED',
                             message='完整音轨响度统一失败，旧成片保留')
    return out, err


def _json_measurement(stderr):
    matches = re.findall(r'\{[^{}]*"input_i"\s*:[^{}]*\}', stderr, re.S)
    try:
        value = json.loads(matches[-1])
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (IndexError, TypeError, ValueError) as exc:
        raise DubForgeError('无法读取完整音轨响度测量结果', code='AUDIO_MASTER_MEASUREMENT_INVALID') from exc


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _graph(filters, label, cut_plan, duration):
    graph, output = list(filters), label
    if not output.startswith('['):
        output = '[' + output + ']'
    if cut_plan:
        from dubforge.pipeline.output_cuts import keep_segments, audio_cut_fade
        segments = keep_segments(cut_plan, 0, duration)
        if not segments:
            raise DubForgeError('完整音轨没有保留内容，请检查成片剪辑', code='AUDIO_MASTER_EMPTY')
        if len(segments) > 1:
            graph.append(output + f'asplit={len(segments)}'
                         + ''.join(f'[amsplit{i}]' for i in range(len(segments))))
        for index, row in enumerate(segments):
            source = f'[amsplit{index}]' if len(segments) > 1 else output
            graph.append(source + f"atrim=start={row['start']:.6f}:end={row['end']:.6f},"
                         f"asetpts=PTS-STARTPTS{audio_cut_fade(cut_plan, row)}[amcut{index}]")
        if len(segments) > 1:
            graph.append(''.join(f'[amcut{i}]' for i in range(len(segments)))
                         + f'concat=n={len(segments)}:v=0:a=1[amjoined]')
            output = '[amjoined]'
        else:
            output = '[amcut0]'
        output_duration = sum(row['end'] - row['start'] for row in segments)
    else:
        output_duration = duration
    graph.append(output + f'atrim=duration={output_duration:.6f},asetpts=PTS-STARTPTS,'
                 'aformat=sample_rates=48000:channel_layouts=stereo[ammix]')
    return graph, output_duration


def _cached(root, key, ctx):
    path, receipt = root / (key + '.flac'), root / (key + '.json')
    try:
        doc = json.loads(receipt.read_text(encoding='utf-8'))
        stat = path.stat()
        if (doc.get('algorithm') != ALGORITHM or doc.get('cache_key') != key
                or doc.get('file') != dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
                or doc.get('sha256') != _hash_file(path, ctx)
                or not isinstance(doc.get('measurement'), dict)
                or not isinstance(doc.get('processing'), dict)
                or not isinstance(doc.get('reference'), dict)):
            return None
        _verify_audio(path, doc['duration'])
        return {**doc, 'path': path, 'cache_hit': True}
    except DubForgeError as exc:
        if exc.code == 'AUDIO_MASTER_OUTPUT_INVALID':
            return None
        raise
    except (OSError, TypeError, ValueError, KeyError):
        return None


def _verify_audio(path, expected_duration):
    """只读取FLAC头部核对时长与声道；缓存另有完整内容校验，不重复全轨解码。"""
    import soundfile as sf
    try:
        info = sf.info(path)
        if (info.format != 'FLAC' or info.samplerate != 48000 or info.channels != 2
                or info.frames <= 0 or abs(info.frames / info.samplerate - expected_duration) > .05):
            raise ValueError()
        return info.frames / info.samplerate
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DubForgeError('完整音轨时长或音频格式校验失败，旧成片保留',
                           code='AUDIO_MASTER_OUTPUT_INVALID') from exc


def prepare_master(ctx, cmd, filters, label, *, cut_plan=None, duration):
    """对完整混音统一响度；输出主音轨可供正式成片与任意试片共同截取。"""
    try:
        raw_target = ctx.cfg('assemble.final_loudness_lufs', -16.)
        raw_ceiling = ctx.cfg('assemble.final_true_peak_db', -1.5)
        if any(isinstance(value, bool) for value in (duration, raw_target, raw_ceiling)):
            raise ValueError()
        duration = float(duration)
        target = float(raw_target)
        ceiling = float(raw_ceiling)
        if not all(math.isfinite(value) for value in (duration, target, ceiling)):
            raise ValueError()
        if duration <= 0 or not -30 <= target <= -9 or not -6 <= ceiling <= 0:
            raise ValueError()
        root = Path(ctx.pdir) / 'audio' / '.master-cache'
        root.mkdir(parents=True, exist_ok=True)
        options = dict(target_lufs=target, true_peak_db=ceiling, headroom_db=_HEADROOM)
        key = _signature(ctx, cmd, filters, label, cut_plan, duration, options)
        with _cache_lock(root / (key + '.lock'), ctx):
            reused = _cached(root, key, ctx)
            if reused is not None:
                if _signature(ctx, cmd, filters, label, cut_plan, duration, options) != key:
                    raise DubForgeError('读取主音轨期间声音输入已变化，请重新合成',
                                       code='AUDIO_MASTER_INPUT_CHANGED')
                report_stage(ctx, 'reusing_audio_master', '复用已完成的完整音轨响度处理', reused=True)
                return reused
            graph, output_duration = _graph(filters, label, cut_plan, duration)
            # 不对LRA作额外收窄；线性处理无法满足峰值空间时才由FFmpeg回退动态处理。
            norm = f'loudnorm=I={target}:TP={ceiling-_HEADROOM}:LRA=50'
            measure_graph = [*graph, '[ammix]' + norm + ':print_format=json[amprobe]']
            report_stage(ctx, 'measuring_uniform_audio', '测量完整混音，计算统一响度方案')
            _, err = _run(ctx, [*cmd, '-filter_complex', ';'.join(measure_graph),
                               '-map', '[amprobe]', '-vn', '-f', 'null', '-'], root)
            first = _json_measurement(err)
            reference = dict(schema=1, integrated_lufs=_finite(first.get('input_i')),
                             true_peak_db=_finite(first.get('input_tp')),
                             loudness_range_lu=_finite(first.get('input_lra')))
            silent = reference['integrated_lufs'] is None
            pre_gain = 0.
            if silent:
                peak = reference['true_peak_db']
                gain = min(0., ceiling - _HEADROOM - peak) if peak is not None else 0.
                normalize_filter = f'volume={gain:.8f}dB'
            else:
                fields = {'measured_I': first.get('input_i'), 'measured_TP': first.get('input_tp'),
                          'measured_LRA': first.get('input_lra'), 'measured_thresh': first.get('input_thresh'),
                          'offset': first.get('target_offset')}
                if any(_finite(value) is None for value in fields.values()):
                    raise DubForgeError('完整音轨测量缺少有效校准参数', code='AUDIO_MASTER_MEASUREMENT_INVALID')
                # loudnorm将精确0的measured_LRA视作“未提供”，即使峰值充足也会回退动态。
                # 只给内部选择器一个远低于0.1LU测量精度的正下限；真实测量仍保存为0。
                fields['measured_LRA'] = max(.001, float(fields['measured_LRA']))
                # 浮点混音可以暂时超过满幅，但measured_I/thresh的FFmpeg参数上限为0。
                # 二遍在浮点域先作等效衰减，并同步平移校准值；保存的reference仍是真实原混音。
                maximum = max(float(fields['measured_I']), float(fields['measured_thresh']))
                if maximum >= 0:
                    pre_gain = -1. - maximum
                    for name in ('measured_I', 'measured_TP', 'measured_thresh'):
                        fields[name] = float(fields[name]) + pre_gain
                normalize_filter = norm + ':' + ':'.join(f'{name}={value}' for name, value in fields.items())
                normalize_filter += ':linear=true:print_format=json'
                if pre_gain:
                    normalize_filter = f'volume={pre_gain:.8f}dB,' + normalize_filter
            pending = root / ('.tmp_' + key + '_' + uuid.uuid4().hex + '.flac')
            path = root / (key + '.flac')
            try:
                report_stage(ctx, 'processing_uniform_audio', '处理完整音轨，保留可用动态并控制峰值')
                _, rendered = _run(ctx, [*cmd, '-filter_complex', ';'.join([
                    *graph, '[ammix]' + normalize_filter + '[ammaster]']), '-map', '[ammaster]',
                    '-vn', '-ar', '48000', '-ac', '2', '-c:a', 'flac', '-sample_fmt', 's32',
                    '-compression_level', '5', '-f', 'flac', str(pending)], root)
                second = {} if silent else _json_measurement(rendered)
                method = 'linear' if silent else str(second.get('normalization_type', '')).lower()
                if method not in {'linear', 'dynamic'}:
                    raise DubForgeError('无法确认完整音轨采用的响度处理方式', code='AUDIO_MASTER_MEASUREMENT_INVALID')
                actual_duration = _verify_audio(pending, output_duration)
                from dubforge.pipeline.audio_quality import measure
                report_stage(ctx, 'verifying_audio_master', '复核完整主音轨实际响度与真峰值')
                measurement = measure(pending, ctx=ctx)
                nominal = None if silent else target - reference['integrated_lufs']
                # FFmpeg线性模式的实际倍率仅为目标I减测量I；offset用于动态模式，不能算入固定增益。
                fixed_gain = gain if silent else nominal
                processing = dict(requested_method='uniform', actual_method=method,
                    gain_db=round(fixed_gain, 6) if method == 'linear' else None,
                    nominal_gain_db=round(nominal, 6) if nominal is not None else None,
                    float_headroom_gain_db=round(pre_gain, 6),
                    peak_limited=False, silent=silent, master_measurement=copy.deepcopy(measurement),
                    target_reached=(not silent and measurement['integrated_lufs'] is not None
                                    and abs(measurement['integrated_lufs'] - target) <= .7),
                    target_delta_lu=(round(measurement['integrated_lufs'] - target, 2)
                                     if measurement['integrated_lufs'] is not None else None))
                _check(ctx)
                # 输入在长处理期间改变时，不将旧方案保存成有效缓存。
                if _signature(ctx, cmd, filters, label, cut_plan, duration, options) != key:
                    raise DubForgeError('处理期间声音输入已变化，请重新合成', code='AUDIO_MASTER_INPUT_CHANGED')
                checksum = _hash_file(pending, ctx)
                atomic_replace(pending, path)
                stat = path.stat()
                doc = dict(algorithm=ALGORITHM, cache_key=key, reference=reference,
                           processing=processing, measurement=measurement, duration=actual_duration,
                           sha256=checksum, file=dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns))
                atomic_write_bytes(root / (key + '.json'), json.dumps(doc, ensure_ascii=False,
                                                                    allow_nan=False).encode())
                return {**doc, 'path': path, 'cache_hit': False}
            finally:
                pending.unlink(missing_ok=True)
    except OSError as exc:
        raise DubForgeError('无法读写完整音轨缓存，请检查磁盘空间和文件权限',
                           code='AUDIO_MASTER_IO', detail=str(exc)) from exc
    except (TypeError, ValueError, OverflowError) as exc:
        raise DubForgeError('完整音轨响度设置或时长无效', code='AUDIO_MASTER_INVALID') from exc
