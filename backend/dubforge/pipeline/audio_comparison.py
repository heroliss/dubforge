"""原片同区间声音对照：只测原音轨，绝不以原片响度替代成片目标。"""
import hashlib
import json
import math
from pathlib import Path

from dubforge.errors import DubForgeError
from dubforge.pipeline import audio_quality
from dubforge.pipeline.output_cuts import audio_cut_fade, keep_segments
from dubforge.utils.fs import atomic_write_bytes


# 测量算法、接缝处理或声道口径变化时必须升版，避免复用旧口径。
ALGORITHM = 'source-ebur128-audio0-native-channels-v1'
METRICS = ('integrated_lufs', 'true_peak_db', 'loudness_range_lu')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _cancel(ctx):
    check = getattr(ctx, 'check_cancel', None)
    if callable(check):
        check()
    marker = getattr(ctx, '_audio_cancel', None)
    if marker is not None and (marker.is_set() if hasattr(marker, 'is_set') else marker()):
        raise DubForgeError('声音对照测量已取消', code='REVIEW_RENDER_CANCELLED')


def _cancelled(error):
    return getattr(error, 'code', '') in {
        'STEP_CANCELLED', 'STEP_TIMEOUT', 'STEP_INTERRUPTED', 'REVIEW_RENDER_CANCELLED',
    } or error.__class__.__name__ in {'CancelledError', 'StepCancelled', 'StepTimeout', 'StepInterrupted'}


def _segments(start, duration, cut_plan, source_segments):
    if not _number(duration) or duration <= 0 or start is not None and (not _number(start) or start < 0):
        raise DubForgeError('声音对照的源视频范围无效', code='AUDIO_COMPARISON_RANGE_INVALID')
    rows = source_segments if source_segments is not None else keep_segments(
        cut_plan, start or 0., (start or 0.) + duration)
    if not isinstance(rows, (tuple, list)) or not rows:
        raise DubForgeError('声音对照没有可用的源视频保留区间', code='AUDIO_COMPARISON_RANGE_INVALID')
    output, previous, result = 0., 0., []
    for index, row in enumerate(rows):
        left, right = row.get('start'), row.get('end')
        if (not _number(left) or not _number(right) or left < 0 or right <= left
                or index and left < previous - 1e-6):
            raise DubForgeError('声音对照的源视频保留区间无效', code='AUDIO_COMPARISON_RANGE_INVALID')
        result.append(dict(start=round(left, 6), end=round(right, 6),
                           output_start=round(output, 6), output_end=round(output + right - left, 6)))
        output += right - left
        previous = right
    return result


def _identity(source):
    stat = source.stat()
    return dict(path=str(source.resolve()), size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def _cache_path(ctx, key):
    root = getattr(ctx, 'pdir', None)
    if root is None:
        return None
    return Path(root) / 'audio' / '.source_measurements' / (key + '.json')


def _read_cache(path, signature):
    if path is None:
        return None
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
        result = doc.get('result')
        if (doc.get('signature') == signature and isinstance(result, dict)
                and result.get('status') in {'ready', 'unavailable'}
                and isinstance(result.get('measurement'), dict)
                and all(key in result['measurement'] and (result['measurement'][key] is None
                        or _number(result['measurement'][key])) for key in METRICS)
                and (result['status'] != 'ready' or _number(result['measurement']['integrated_lufs']))):
            return {**result, 'cache_hit': True}
    except (OSError, ValueError, TypeError):
        pass
    return None


def _unavailable(reason, code):
    return dict(status='unavailable', measurement=dict.fromkeys(METRICS), reason=reason,
                reason_code=code, cache_hit=False)


def _source_measurement(ctx, source, segments, cut_plan):
    before = _identity(source)
    fades = [audio_cut_fade(cut_plan, row) for row in segments]
    signature = dict(algorithm=ALGORITHM, source=before, segments=segments, fades=fades)
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    cache_path = _cache_path(ctx, key)
    cached = _read_cache(cache_path, signature)
    if cached is not None:
        return cached
    _cancel(ctx)
    out, _ = audio_quality._run(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
        '-show_entries', 'stream=index', '-of', 'json', str(source)], ctx)
    if not json.loads(out).get('streams'):
        result = _unavailable('原片没有音轨，无法进行声音对照。', 'no_audio')
    else:
        count = len(segments)
        graph = []
        if count > 1:
            graph.append(f'[0:a:0]asplit={count}' + ''.join(f'[srcsplit{i}]' for i in range(count)))
        for index, row in enumerate(segments):
            label = f'[srcsplit{index}]' if count > 1 else '[0:a:0]'
            graph.append(label + f"atrim=start={row['start']:.6f}:end={row['end']:.6f},"
                         f'asetpts=PTS-STARTPTS{fades[index]}[srccut{index}]')
        label = '[srccut0]'
        if count > 1:
            graph.append(''.join(f'[srccut{i}]' for i in range(count)) + f'concat=n={count}:v=0:a=1[srcjoined]')
            label = '[srcjoined]'
        measured = audio_quality.measure_mix(ctx,
            ['ffmpeg', '-hide_banner', '-nostats', '-threads', '1', '-i', str(source)], graph, label)
        values = {key: measured.get(key) for key in METRICS}
        if values['integrated_lufs'] is None:
            result = {**_unavailable('原片同范围接近静音，未测得有效综合响度。', 'silent'), 'measurement': values}
        else:
            result = dict(status='ready', measurement=values, reason='', cache_hit=False)
    _cancel(ctx)
    if _identity(source) != before:
        raise DubForgeError('测量期间原视频发生变化，本次原片对照未保存。', code='AUDIO_COMPARISON_SOURCE_CHANGED')
    if cache_path is not None:
        try:
            atomic_write_bytes(cache_path, json.dumps(dict(signature=signature, result=result),
                ensure_ascii=False, allow_nan=False).encode('utf-8'))
        except OSError:
            # 缓存仅用于省去重复扫描，不能因缓存目录暂不可写丢掉有效测量。
            pass
    return result


def _assessment(report, duration):
    settings = report.get('settings') or {}
    actual, target = report.get('integrated_lufs'), settings.get('target_lufs')
    delta = round(actual - target, 2) if _number(actual) and _number(target) else None
    if report.get('preview') is True:
        status, message = 'preview', '这是区域试片；沿用整片声音处理，不据此判断完整成片是否达到目标响度。'
        delta = None
    elif settings.get('mode', 'off') == 'off':
        status, message = 'off', '当前保持混音响度；如需不同项目音量一致，请启用最终成片响度校准。'
    elif delta is None or report.get('has_audio') is False:
        status, message = 'unmeasurable', '没有可测量的有效响度，无法核对目标。'
    elif abs(delta) <= .7:
        status, message = 'reached', '实际成片响度已接近目标。'
    elif (report.get('processing') or {}).get('peak_limited'):
        status, message = 'peak_limited', '为保护真峰值而限制了静态增益，实际响度未达到目标。'
    elif delta < 0:
        status, message = 'below', '实际成片响度低于目标，请结合试听和峰值余量核对。'
    else:
        status, message = 'above', '实际成片响度高于目标，请核对实际声音处理结果。'
    return dict(target_status=status, target_delta_lu=delta,
                lra_reliable=duration >= 60 and _number(report.get('loudness_range_lu')), message=message)


def augment_report(ctx, report, source, *, start=None, duration, cut_plan=None,
                   full_duration=None, source_segments=None):
    """附加同一时间范围的真实原片对照；普通对照失败不丢掉有效成片，取消除外。"""
    result = dict(report)
    _cancel(ctx)
    try:
        segments = _segments(start, duration, cut_plan, source_segments)
        if _number(full_duration) and segments[-1]['end'] > full_duration + 1e-6:
            raise DubForgeError('声音对照的范围超出原片时长', code='AUDIO_COMPARISON_RANGE_INVALID')
        total = round(sum(row['end'] - row['start'] for row in segments), 6)
        result['scope'] = dict(kind='preview' if report.get('preview') is True or start is not None else 'full',
            source_start=segments[0]['start'], source_end=segments[-1]['end'],
            source_segments=segments, output_duration=total)
        if _number(full_duration):
            result['scope']['full_source_duration'] = full_duration
        if result['scope']['kind'] == 'preview':
            result['preview'] = True
            result['target_reached'] = None
        if hasattr(ctx, 'report_progress'):
            from dubforge.execution_progress import report_stage
            report_stage(ctx, 'measuring_source_comparison', '测量原片同范围响度对照')
        reference = _source_measurement(ctx, Path(source), segments, cut_plan)
    except Exception as error:
        if _cancelled(error):
            raise
        _cancel(ctx)
        total = result.get('scope', {}).get('output_duration', duration)
        reference = dict(status='failed', measurement=dict.fromkeys(METRICS), cache_hit=False,
            reason='原片声音对照暂未完成，成片及其实际测量已保留，可重新生成或补测。',
            error_code=getattr(error, 'code', 'AUDIO_COMPARISON_FAILED'))
        log = getattr(ctx, 'log', None)
        if callable(log):
            log('原片声音对照未完成；有效成片保留。')
    result['source_comparison'] = reference
    measured = reference['measurement']
    comparable = reference['status'] == 'ready' and _number(report.get('integrated_lufs'))
    lra_comparable = comparable and total >= 60 and _number(report.get('loudness_range_lu')) and _number(measured['loudness_range_lu'])
    def delta(key):
        return round(report[key] - measured[key], 2) if comparable and _number(report.get(key)) and _number(measured.get(key)) else None
    result['comparison'] = dict(available=comparable, integrated_delta_lu=delta('integrated_lufs'),
        true_peak_delta_db=delta('true_peak_db'),
        loudness_range_delta_lu=delta('loudness_range_lu') if lra_comparable else None,
        lra_comparable=bool(lra_comparable))
    result['assessment'] = _assessment(result, total)
    return result
