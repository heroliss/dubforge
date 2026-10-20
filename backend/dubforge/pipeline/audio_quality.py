"""对实际编码音轨测量响度/真峰值，并以保守静态增益完成成片声音控制。"""
import json
import math
import re
import subprocess
import uuid
from pathlib import Path
import numpy as np

from dubforge.errors import DubForgeError
from dubforge.utils.fs import atomic_replace, atomic_write_bytes

FINAL_KEYS = {'assemble.final_loudness_mode', 'assemble.final_loudness_lufs', 'assemble.final_true_peak_db'}
DEFAULTS = {'assemble.final_loudness_mode': 'off', 'assemble.final_loudness_lufs': -16.,
            'assemble.final_true_peak_db': -1.5}


def settings(cfg):
    values = {key: cfg(key, default) for key, default in DEFAULTS.items()}
    mode = values['assemble.final_loudness_mode']
    target, ceiling = values['assemble.final_loudness_lufs'], values['assemble.final_true_peak_db']
    if (mode not in {'off', 'normalize', 'uniform'} or type(target) not in (int, float)
            or not math.isfinite(target) or not -30 <= target <= -9
            or type(ceiling) not in (int, float) or not math.isfinite(ceiling) or not -6 <= ceiling <= 0):
        raise DubForgeError('成片响度设置无效，请检查目标响度和真峰值上限', code='AUDIO_LOUDNESS_INVALID')
    return dict(mode=mode, target_lufs=float(target), true_peak_db=float(ceiling))


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _run(cmd, ctx=None):
    from dubforge.pipeline.render_resources import bounded_ffmpeg_command, ffmpeg_failure
    cmd = bounded_ffmpeg_command(cmd)
    if ctx is not None and hasattr(ctx, 'run_subprocess'):
        code, out, err = ctx.run_subprocess(cmd)
    else:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=7200, check=False)
        code, out, err = result.returncode, result.stdout, result.stderr
    if code:
        raise ffmpeg_failure(err, output_dir=getattr(ctx, 'pdir', None),
                             fallback_code='AUDIO_PROCESS_FAILED',
                             message='声音测量或处理失败，请检查音轨与 FFmpeg 日志')
    return out, err


MEASURE_FILTER = 'silencedetect=n=-50dB:d=2,ebur128=peak=true:framelog=verbose'
MIX_MEASURE_ALGORITHM = 'full-mix-ebur128-silence-native-channels-v1'
_AAC_PEAK_ATTEMPTS = 3
_AAC_PEAK_HEADROOM_DB = .3
_AAC_PEAK_MAX_ATTENUATION_DB = 24.
_AAC_PEAK_TOLERANCE_DB = .05


def _measurement(err):
    summary = err.rsplit('Summary:', 1)[-1]
    integrated = re.search(r'Integrated loudness:\s*I:\s*([^\s]+)', summary)
    lra = re.search(r'Loudness range:\s*LRA:\s*([^\s]+)', summary)
    peak = re.search(r'True peak:\s*Peak:\s*([^\s]+)', summary)
    if not integrated or not peak:
        raise DubForgeError('无法读取声音响度测量结果', code='AUDIO_MEASUREMENT_INVALID')
    raw = dict(input_i=integrated[1], input_tp=peak[1], input_lra=lra[1] if lra else None)
    integrated_value = _finite(raw['input_i'])
    if _finite(raw['input_tp']) is None or integrated_value is None or integrated_value <= -69.9:
        raw['input_i'] = None
    silence, pending = [], None
    for match in re.finditer(r'silence_(start|end):\s*([\d.]+)', err):
        if match[1] == 'start':
            pending = float(match[2])
        elif pending is not None:
            silence.append(dict(start=round(pending, 3), end=round(float(match[2]), 3)))
            pending = None
    # astats 的 Peak count 不是削顶样本数；只依据接近满幅的峰值提示风险。
    true_peak = _finite(raw.get('input_tp'))
    issues = []
    if true_peak is not None and true_peak >= -.05:
        issues.append(dict(kind='peak_risk', severity='warning',
                           message='音轨峰值接近或超过满幅，请试听核对；该测量不能证明存在可闻失真。'))
    if _finite(raw.get('input_i')) is None:
        issues.append(dict(kind='silent_audio', severity='warning', message='音轨为静音或不足以测量综合响度。'))
    return dict(schema=1, integrated_lufs=_finite(raw.get('input_i')), true_peak_db=true_peak,
                loudness_range_lu=_finite(raw.get('input_lra')), silence_ranges=silence,
                trailing_silence_start=pending, issues=issues)


def measure(path, *, ctx=None):
    """测量真实音频；静音的 -inf 以 null 返回，不伪造 0 LUFS。"""
    _, err = _run(['ffmpeg', '-hide_banner', '-nostats', '-threads', '1', '-i', str(path),
        '-map', '0:a:0', '-vn', '-af', MEASURE_FILTER, '-f', 'null', '-'], ctx)
    return _measurement(err)


def _read_mix_measurement(path, key):
    def number(value):
        return type(value) in (int, float) and math.isfinite(value)
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
        report = doc.get('measurement')
        metrics = ('integrated_lufs', 'true_peak_db', 'loudness_range_lu')
        if (doc.get('algorithm') == MIX_MEASURE_ALGORITHM and doc.get('cache_key') == key
                and isinstance(report, dict) and report.get('schema') == 1
                and all(name in report and (report[name] is None
                        or number(report[name]))
                        for name in metrics)
                and isinstance(report.get('silence_ranges'), list)
                and all(isinstance(row, dict) and number(row.get('start'))
                        and number(row.get('end')) and 0 <= row['start'] <= row['end']
                        for row in report['silence_ranges'])
                and 'trailing_silence_start' in report and (report['trailing_silence_start'] is None
                        or number(report['trailing_silence_start']))
                and isinstance(report.get('issues'), list)
                and all(isinstance(issue, dict) for issue in report['issues'])):
            return report
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _measure_mix_command(ctx, command, *, signature):
    """只复用未施加最终增益的混音测量；目标响度变化仍须重新计算增益。"""
    from contextlib import ExitStack
    from dubforge.pipeline.audio_master import _cache_lock, _check, _signature
    _check(ctx)
    root = getattr(ctx, 'pdir', None)
    if root is None:
        _, err = _run(command, ctx)
        _check(ctx)
        return _measurement(err)
    # 缓存目录无法使用时仍可完成有效测量；失败与取消不会写入缓存。
    try:
        root = Path(root) / 'audio' / '.mix-measurements'
        root.mkdir(parents=True, exist_ok=True)
        key = _signature(ctx, *signature, {}, algorithm=MIX_MEASURE_ALGORITHM)
    except OSError:
        _, err = _run(command, ctx)
        _check(ctx)
        return _measurement(err)
    with ExitStack() as stack:
        try:
            stack.enter_context(_cache_lock(root / (key + '.lock'), ctx))
        except OSError:
            # 缓存权限问题只影响复用，不应阻止本次合成。
            _, err = _run(command, ctx)
            _check(ctx)
            return _measurement(err)
        path = root / (key + '.json')
        cached = _read_mix_measurement(path, key)
        if cached is not None:
            _check(ctx)
            if _signature(ctx, *signature, {}, algorithm=MIX_MEASURE_ALGORITHM) == key:
                from dubforge.execution_progress import report_stage
                report_stage(ctx, 'reusing_mix_measurement', '复用完整混音响度测量，重新计算当前增益', reused=True)
                return cached
            raise DubForgeError('读取混音测量期间声音输入已变化，请重新合成', code='AUDIO_MIX_INPUT_CHANGED')
        _, err = _run(command, ctx)
        report = _measurement(err)
        _check(ctx)
        if _signature(ctx, *signature, {}, algorithm=MIX_MEASURE_ALGORITHM) != key:
            raise DubForgeError('测量期间声音输入已变化，请重新合成', code='AUDIO_MIX_INPUT_CHANGED')
        try:
            atomic_write_bytes(path, json.dumps(dict(algorithm=MIX_MEASURE_ALGORITHM,
                cache_key=key, measurement=report), ensure_ascii=False, allow_nan=False).encode('utf-8'))
        except OSError:
            pass
        return report


def measure_mix(ctx, cmd, filters, label, *, cut_plan=None, duration=None):
    """只解码声音测量完整混音，不复制或编码整段视频；试片复用整片增益。"""
    graph = list(filters)
    if cut_plan:
        from dubforge.pipeline.output_cuts import keep_segments, audio_cut_fade
        segments = keep_segments(cut_plan, 0, duration)
        if len(segments) > 1:
            graph.append(label + f'asplit={len(segments)}' + ''.join(f'[aqsplit{i}]' for i in range(len(segments))))
        for index, row in enumerate(segments):
            source = f'[aqsplit{index}]' if len(segments) > 1 else label
            soften = audio_cut_fade(cut_plan, row)
            graph.append(f"{source}atrim=start={row['start']:.6f}:end={row['end']:.6f},asetpts=PTS-STARTPTS{soften}[aqcut{index}]")
        if len(segments) > 1:
            graph.append(''.join(f'[aqcut{i}]' for i in range(len(segments))) + f'concat=n={len(segments)}:v=0:a=1[aqjoined]')
            label = '[aqjoined]'
        else:
            label = '[aqcut0]'
    graph.append(label + MEASURE_FILTER + '[aqmeasured]')
    command = [*cmd, '-filter_complex', ';'.join(graph), '-map', '[aqmeasured]', '-vn', '-f', 'null', '-']
    return _measure_mix_command(ctx, command,
        signature=(cmd, graph, '[aqmeasured]', cut_plan, duration))


def static_gain(measurement, target_lufs, true_peak_db, *, headroom=.3):
    """固定增益同时受目标响度和峰值余量约束，不压缩声音起伏。"""
    loudness, peak = measurement.get('integrated_lufs'), measurement.get('true_peak_db')
    available = true_peak_db - headroom - peak if peak is not None else None
    if loudness is None or peak is None:
        return dict(gain_db=min(0., available) if available is not None else 0.,
                    requested_gain_db=None, available_gain_db=available, headroom_db=headroom,
                    peak_limited=False, silent=True)
    requested = target_lufs - loudness
    gain = min(requested, available)
    return dict(gain_db=round(gain, 6), requested_gain_db=round(requested, 6),
                available_gain_db=round(available, 6), headroom_db=headroom,
                peak_limited=gain < requested - .05, silent=False)


def annotate_lines(lines, audios):
    """供质量页定位句子；峰值风险不是可闻失真的诊断结论。"""
    levels = [row['active_rms_db'] + row.get('gain_db', 0) + row.get('leveling_gain_db', 0)
              for row in lines if row.get('active_rms_db') is not None]
    median = float(np.median(levels)) if levels else None
    result = []
    for line, audio in zip(lines, audios):
        data = np.asarray(audio)
        peak = float(np.max(np.abs(data))) if len(data) else 0.
        near_full = int(np.count_nonzero(np.abs(data) >= .999))
        issues = []
        if line.get('active_rms_db') is None:
            issues.append(dict(kind='silent_line', severity='warning', message='该句声音接近静音，请试听核对。'))
        if near_full:
            issues.append(dict(kind='sample_peak_risk', severity='info',
                message='句子含接近满幅的采样点；不代表已发生可闻削波，请结合实际成片峰值和试听判断。'))
        effective_gain = line.get('gain_db', 0) + line.get('leveling_gain_db', 0)
        effective_rms = line['active_rms_db'] + effective_gain if line.get('active_rms_db') is not None else None
        if median is not None and effective_rms is not None and abs(effective_rms - median) > 8:
            issues.append(dict(kind='level_difference', severity='info', message='该句音量与多数句子差异较大，请核对是否属于轻声或情绪表达。'))
        if line.get('gain_stale'):
            issues.append(dict(kind='gain_stale', severity='warning', message='该句选用声音已变化，旧音量精调未自动应用。'))
        result.append({**line, 'sample_peak_db': 20 * math.log10(peak) if peak > 0 else None,
                       'effective_gain_db': round(effective_gain, 2), 'effective_rms_db': effective_rms,
                       'near_full_scale_samples': near_full, 'issues': issues})
    return result


def _gain_file(source, target, gain, *, video, ctx=None):
    cmd = ['ffmpeg', '-y', '-hide_banner', '-nostats', '-threads', '1', '-i', str(source)]
    if video:
        cmd += ['-map', '0:v:0', '-map', '0:a:0', '-map_metadata', '0', '-map_chapters', '0',
                '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-af', f'volume={gain:.8f}dB',
                '-movflags', '+faststart', '-f', 'mp4', str(target)]
    else:
        cmd += ['-vn', '-af', f'volume={gain:.8f}dB', '-ar', '32000', '-ac', '1',
                '-c:a', 'pcm_f32le', '-f', 'wav', str(target)]
    _run(cmd, ctx)


def normalize_voice(path, ctx):
    """保留配音归一开关，但只作测量后的静态增益，不逐次动态压缩。"""
    report = measure(path, ctx=ctx)
    gain = static_gain(report, -16., -1.5)
    target = Path(path).with_name('.tmp_voice_norm_' + Path(path).name)
    try:
        _gain_file(path, target, gain['gain_db'], video=False, ctx=ctx)
        atomic_replace(target, path)
    finally:
        target.unlink(missing_ok=True)
    return {**report, **gain}


def final_gain(ctx, measurement):
    options = settings(ctx.cfg)
    return static_gain(measurement, options['target_lufs'], options['true_peak_db'])


def gain_filter(label, treatment, output='normalized_audio'):
    source = label if label.startswith('[') else '[' + label + ']'
    return f"{source}volume={treatment['gain_db']:.8f}dB[{output}]", '[' + output + ']'


def _audio_log(ctx, message):
    log = getattr(ctx, 'log', None)
    if callable(log):
        log(message)


def _level_text(value, unit):
    return '无法测量' if value is None else f'{value:.2f} {unit}'


def _correct_encoded_peak(ctx, path, report, options, treatment):
    """每轮从同一编码候选反馈降增益；复测通过之前不替换候选。"""
    from dubforge.pipeline.audio_master import _check
    from dubforge.execution_progress import report_stage
    ceiling = options['true_peak_db']
    diagnostic = dict(target_true_peak_db=ceiling, initial_true_peak_db=report['true_peak_db'],
                      initial_integrated_lufs=report['integrated_lufs'],
                      max_attempts=_AAC_PEAK_ATTEMPTS,
                      max_attenuation_db=_AAC_PEAK_MAX_ATTENUATION_DB, attempts=[])
    corrected = Path(path).with_name('.tmp_peak_' + uuid.uuid4().hex + '_' + Path(path).name)
    correction = 0.
    try:
        for attempt in range(1, _AAC_PEAK_ATTEMPTS + 1):
            _check(ctx)
            # 反馈累计施加到原候选，避免把多轮 AAC 的编码误差和损失继续串联。
            next_correction = max(-_AAC_PEAK_MAX_ATTENUATION_DB,
                correction + ceiling - report['true_peak_db'] - _AAC_PEAK_HEADROOM_DB)
            if next_correction >= correction:
                break
            correction = next_correction
            _audio_log(ctx, f'AAC 真峰值纠偏 {attempt}/{_AAC_PEAK_ATTEMPTS}：'
                       f'从原始候选累计衰减 {-correction:.2f} dB，上限 {ceiling:.2f} dBTP。')
            if hasattr(ctx, 'report_progress'):
                report_stage(ctx, 'correcting_output_audio', '复核并纠正 AAC 编码真峰值',
                             completed=attempt - 1, total=_AAC_PEAK_ATTEMPTS, unit='轮')
            _gain_file(path, corrected, correction, video=True, ctx=ctx)
            _check(ctx)
            report = measure(corrected, ctx=ctx)
            _check(ctx)
            diagnostic['attempts'].append(dict(attempt=attempt, gain_db=round(correction, 6),
                true_peak_db=report['true_peak_db'], integrated_lufs=report['integrated_lufs']))
            _audio_log(ctx, f'AAC 真峰值纠偏 {attempt} 复测：'
                       f"{_level_text(report['integrated_lufs'], 'LUFS')} / "
                       f"{_level_text(report['true_peak_db'], 'dBTP')}。")
            # 原候选有明确超限峰值，有限衰减后失去测量不能当作核验通过。
            if report['true_peak_db'] is None:
                raise DubForgeError('AAC 纠偏后无法读取有效真峰值，已停止保存并保留旧成片',
                                    code='AUDIO_MEASUREMENT_INVALID', detail=diagnostic)
            if report['true_peak_db'] <= ceiling + _AAC_PEAK_TOLERANCE_DB:
                _check(ctx)
                atomic_replace(corrected, path)
                if type(treatment.get('gain_db')) in (int, float):
                    treatment['gain_db'] += correction
                treatment['aac_peak_correction_db'] = correction
                treatment['aac_peak_verification'] = diagnostic
                return report
        message = (f'编码后的真峰值仍超出上限（目标 {ceiling:.2f} dBTP，'
                   f"实测 {_level_text(report['true_peak_db'], 'dBTP')}，"
                   f"已自动纠偏 {len(diagnostic['attempts'])} 次）；旧成片保留，请查看声音复核日志")
        _audio_log(ctx, message)
        raise DubForgeError(message, code='AUDIO_TRUE_PEAK_EXCEEDED', detail=diagnostic,
                            suggestion='请检查声音复核日志中的初次编码和各轮实测值，再重试合成。')
    finally:
        corrected.unlink(missing_ok=True)


def finalize(ctx, path, *, has_audio=True, line_report=None, reference=None,
             applied=None, preview=False):
    """只检验实际 AAC，主增益须在第一次 AAC 编码前通过无损混音测量应用。"""
    clear_eta = getattr(ctx, "clear_phase_eta", None)
    if callable(clear_eta):
        clear_eta()
    options = settings(ctx.cfg)
    if not has_audio:
        return dict(schema=1, has_audio=False, preview=bool(preview), settings=options, issues=[])
    from dubforge.execution_progress import report_stage
    from dubforge.pipeline.audio_master import _check
    _check(ctx)
    if hasattr(ctx, 'report_progress'):
        report_stage(ctx, 'measuring_output_audio', '测量成片实际响度与真峰值')
    report = measure(path, ctx=ctx)
    _check(ctx)
    expectation = ('响度处理已关闭；实际编码 ' if options['mode'] == 'off' else
                   f"目标 {options['target_lufs']:.2f} LUFS / "
                   f"真峰值上限 {options['true_peak_db']:.2f} dBTP；初次编码 ")
    _audio_log(ctx, '成片音频复核：' + expectation +
               f"{_level_text(report['integrated_lufs'], 'LUFS')} / "
               f"{_level_text(report['true_peak_db'], 'dBTP')}。")
    treatment = dict(applied or dict(gain_db=0., peak_limited=False,
                                     silent=report['integrated_lufs'] is None))
    if options['mode'] != 'off':
        if applied is None or reference is None:
            raise DubForgeError('成片缺少编码前的完整混音测量，已停止保存', code='AUDIO_MEASUREMENT_REQUIRED')
        # AAC 瞬态漂移须按实际编码反馈复测；尝试次数和累计衰减均有限。
        if report['true_peak_db'] is not None and report['true_peak_db'] > options['true_peak_db']:
            report = _correct_encoded_peak(ctx, path, report, options, treatment)
    reached = (report['integrated_lufs'] is not None
               and abs(report['integrated_lufs'] - options['target_lufs']) <= .7)
    result = dict(**report, has_audio=True, preview=bool(preview), settings=options, processing=treatment,
                  target_reached=reached if options['mode'] != 'off' and not preview else None,
                  line_report=line_report or [])
    if reference:
        result['reference'] = dict(scope='full_output_mix', integrated_lufs=reference['integrated_lufs'],
                                   true_peak_db=reference['true_peak_db'])
    if treatment.get('master_measurement'):
        result['full_output_measurement'] = treatment['master_measurement']
    if options['mode'] != 'off' and treatment['peak_limited']:
        result['issues'] = [*result['issues'], dict(kind='peak_limited', severity='info',
            message='为保留声音起伏并遵守真峰值上限，未强行放大到目标响度。')]
    return result


def report_path(path):
    return Path(path).with_name(Path(path).name + '.audio-report.json')


def write_report(path, report):
    path = Path(path)
    stat = path.stat()
    value = {**report, 'file': dict(name=path.name, size=stat.st_size, mtime_ns=stat.st_mtime_ns)}
    atomic_write_bytes(report_path(path), json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
    return value


def read_report(path):
    path = Path(path)
    try:
        doc = json.loads(report_path(path).read_text(encoding='utf-8'))
        if not isinstance(doc, dict):
            return None
        stat = path.stat()
        if doc.get('file') != dict(name=path.name, size=stat.st_size, mtime_ns=stat.st_mtime_ns):
            return None
        return doc
    except (OSError, ValueError, TypeError):
        return None
