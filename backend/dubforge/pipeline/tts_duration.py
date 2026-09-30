"""IndexTTS 的超长重试：按预设因子缩短，保留本轮最短的完整音频。"""
import math
import re

import soundfile as sf

from dubforge.errors import ConfigError, DubForgeError
from dubforge.utils.fs import atomic_replace

KEY = 'tts.indextts25.duration_retry_factors'
EPSILON_SECONDS = .05  # 忽略音频/字幕计时取整的毫秒级差异。


def parse_factors(value):
    if value is None or value == '':
        return []
    if not isinstance(value, str):
        raise ConfigError('超长重试因子请填写用逗号分隔的数字，如 0.7, 0.5；留空关闭。')
    try:
        values = [float(part) for part in re.split(r'[,，;；\s]+', value.strip()) if part]
        if any(not math.isfinite(v) or not .5 <= v <= 2 for v in values):
            raise ValueError
        return values
    except ValueError as exc:
        raise ConfigError('超长重试因子必须是 0.5～2 的数字，用逗号分隔，如 0.7, 0.5；留空关闭。') from exc


def attempt_factors(initial, value):
    """每次都与当时实际使用的因子比较，跳过相等、重复或更大的值。"""
    result = [float(initial)]
    for factor in parse_factors(value):
        if factor < result[-1]:
            result.append(factor)
    return result


def audio_seconds(path):
    try:
        seconds = float(sf.info(str(path)).duration)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('音频时长为空')
        return seconds
    except (RuntimeError, ValueError) as exc:
        raise DubForgeError('无法读取合成音频的有效时长', code='TTS_AUDIO_INVALID', detail=str(exc)) from exc


def synthesize(synth, out, line, factors, *, log, on_attempt, can_retry):
    """synth(path, factor) 仅负责调用模型；失败/停止重试不损坏已生成的音频。"""
    available = max(0.0, float(line.get('available_duration',
        float(line['end']) - float(line['start']) + float(line.get('gap', 0)))))
    info = {'available': available, 'attempts': [], 'used_factor': factors[0]}
    candidate = out.with_suffix('.retry.wav.tmp')
    best = out.with_suffix('.best.wav.tmp')
    best_seconds = None
    try:
        for index, factor in enumerate(factors):
            if index:
                if not can_retry():
                    break
                log(f"句 {line['id']} 配音 {best_seconds:.2f}s，可用 {available:.2f}s；"
                    f"缩时重试 {index}/{len(factors) - 1}，时长因子 {factor:g}")
            on_attempt(index, len(factors), factor)
            info['retries'] = index
            path = candidate
            try:
                synth(path, factor)
                seconds = audio_seconds(path)
            except Exception as exc:
                if isinstance(exc, DubForgeError) and exc.code in {
                        'STEP_CANCELLED', 'STEP_TIMEOUT', 'TTS_REQUEST_TIMEOUT',
                        'TTS_SERVICE_DRAINING', 'TTS_REQUEST_FAILED', 'SERVICE_OFFLINE'}:
                    raise  # 停止/服务超时不能被重试兜底吞掉并继续整批任务。
                if index == 0:
                    raise
                info['error'] = str(exc)
                log(f"句 {line['id']} 缩时重试失败，保留已生成音频：{exc}")
                break
            info['attempts'].append({'factor': factor, 'seconds': round(seconds, 3)})
            if best_seconds is None or seconds < best_seconds:
                # 缩时尝试尚未结束时保留正式旧版本，连续预览不会读到中间结果。
                atomic_replace(candidate, best)
                best_seconds = seconds
                info['used_factor'] = factor
            if best_seconds <= available + EPSILON_SECONDS:
                break
            if best_seconds <= available + max(EPSILON_SECONDS, min(.15, available * .05)):
                info['retry_skipped'] = 'minor_overrun'
                log(f"句 {line['id']} 轻微超长 {best_seconds - available:.2f}s，"
                    '不重复调用模型；保留完整音频和超长提示')
                break
        info['seconds'] = round(best_seconds, 3)
        info['overrun'] = best_seconds > available + EPSILON_SECONDS
        if info['overrun']:
            log(f"句 {line['id']} 配音仍超长 {best_seconds - available:.2f}s，保留完整音频，需试听或调整译文/时间")
        atomic_replace(best, out)
        return info
    finally:
        candidate.unlink(missing_ok=True)
        best.unlink(missing_ok=True)
