"""成片声音后期：逐句增益与声音绑定，和模型生成、播放位置相互独立。"""
import copy
import hashlib
import json
import math

import numpy as np

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.line_settings import identity

KEY = 'audio_line_gains'


def records(project):
    value = crud.project_params(project).get(KEY, {}) if hasattr(project, 'params') else {}
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def binding(line, audio_version=None):
    chosen = line.get('selection') or line
    return _digest([identity(line), chosen.get('source', 'dub'), chosen.get('take'), audio_version])


def line_state(project, line, *, audio_version=None, saved=None):
    """音频版本改变后保留旧调音但不自动套用；revision 用于并发保护。"""
    rows = records(project) if saved is None else saved
    record = rows.get(str(line['id']))
    token = binding(line, audio_version)
    valid = (isinstance(record, dict) and record.get('binding') == token
             and type(record.get('gain_db')) in (int, float)
             and math.isfinite(record['gain_db']) and -12 <= record['gain_db'] <= 12)
    chosen = line.get('selection') or line
    available = chosen.get('source', 'dub') != 'silent' and audio_version not in (None, '0')
    return dict(gain_db=float(record['gain_db']) if valid else 0.,
                effective_gain_db=float(record['gain_db']) if valid else 0.,
                saved_gain_db=record.get('gain_db') if isinstance(record, dict) else None,
                active=bool(valid and record['gain_db']), stale=record is not None and not valid,
                binding=token, revision=_digest([token, record]), line_identity=identity(line),
                source=chosen.get('source', 'dub'), slot=chosen.get('take'),
                audio_version=copy.deepcopy(audio_version), available=available)


def save_line(project, line, body, *, audio_version=None):
    """由 API 在项目编辑门禁内调用；修改后期增益不改 WAV 与满意标记。"""
    if not isinstance(body, dict) or set(body) - {'gain_db', 'revision', 'binding', 'reset'}:
        raise DubForgeError('句子音量修改格式无效', code='AUDIO_GAIN_INVALID')
    state = line_state(project, line, audio_version=audio_version)
    if body.get('revision') != state['revision'] or body.get('binding') != state['binding']:
        raise DubForgeError('本句或选用声音已变化，请重新打开后保存', code='AUDIO_GAIN_CONFLICT')
    if body.get('reset') is not True and not state['available']:
        raise DubForgeError('请先选择已有声音，再调整这句音量；静音和待生成句没有可绑定的声音',
                            code='AUDIO_GAIN_UNAVAILABLE')
    gain = body.get('gain_db')
    if body.get('reset') is not True and (type(gain) not in (int, float)
            or not math.isfinite(gain) or not -12 <= gain <= 12):
        raise DubForgeError('句子音量须为 −12 至 +12 dB', code='AUDIO_GAIN_INVALID')
    params = crud.project_params(project)
    rows = records(project)
    if body.get('reset') is True or not gain:
        rows.pop(str(line['id']), None)
    else:
        rows[str(line['id'])] = dict(binding=state['binding'], gain_db=round(float(gain), 2))
    if rows:
        params[KEY] = rows
    else:
        params.pop(KEY, None)
    project.params = json.dumps(params, ensure_ascii=False)
    crud.save_project(project)
    crud.write_manifest(project.uid)
    return line_state(project, line, audio_version=audio_version)


def audio_version_for_line(line, pdir, *, dependencies=None):
    from dubforge.pipeline.dubbing_edit import file_version, take_path
    from pathlib import Path
    chosen = line.get('selection') or line
    source, take = chosen.get('source', 'dub'), chosen.get('take')
    if source == 'dub' and take is not None:
        from dubforge.pipeline.dubbing_edit import take_name
        path = (dependencies.path('tts', take_name(line['id'], take)) if dependencies else None)
        path = path or take_path(Path(pdir), line['id'], take)
    elif source == 'original':
        path = dependencies.path('preprocess', 'vocal.wav') if dependencies else None
        path = path or Path(pdir) / 'audio' / 'vocal.wav'
    else:
        path = None
    return file_version(path) if path else None


def line_view(project, lines, pdir, *, dependencies=None):
    """工作台已有行可直接调用；仅读取文件 stat，不解码音频、不启动模型。"""
    saved = records(project)
    result = {}
    for line in lines:
        result[str(line['id'])] = line_state(project, line,
            audio_version=audio_version_for_line(line, pdir, dependencies=dependencies), saved=saved)
    return result


def report_view(project, artifact_name):
    """只读已登记成片报告；不允许相对路径跳转，也不在 GET 触发全片测量。"""
    from pathlib import Path
    from dubforge.pipeline.audio_quality import read_report
    if not isinstance(artifact_name, str) or Path(artifact_name).name != artifact_name:
        raise DubForgeError('成片文件名称无效', code='AUDIO_REPORT_INVALID')
    artifact = crud.latest_artifacts(project.id, ['assemble']).get('assemble/' + artifact_name)
    if artifact is None:
        raise DubForgeError('成片尚未登记或已被移除', code='AUDIO_REPORT_NOT_FOUND')
    root = crud.project_dir_by_project(project)
    path = Path(artifact.rel_path)
    path = path if path.is_absolute() else root / path
    return read_report(path)


def active_rms(audio):
    """排除静音的保守能量估计；不把留白当作需要放大的轻声。"""
    data = np.asarray(audio, dtype=np.float32)
    if not len(data):
        return None
    peak = float(np.max(np.abs(data)))
    if peak < .001:
        return None
    # 50ms 块，保留高于全句峰值 35dB 且高于 −55dBFS 的块。
    width = 1600
    blocks = [float(np.sqrt(np.mean(data[i:i + width].astype(np.float64) ** 2)))
              for i in range(0, len(data), width)]
    active = [v for v in blocks if v >= max(10 ** (-55 / 20), peak * 10 ** (-35 / 20))]
    return 20 * math.log10(math.sqrt(sum(v * v for v in active) / len(active))) if active else None


def balance_entries(entries, *, enabled=False, max_db=3.):
    """只对明显句间差异作有限修正；静音不放大，±2dB 情绪差异保留。"""
    levels = [active_rms(audio) for _, audio in entries]
    voiced = [v for v in levels if v is not None]
    target = float(np.median(voiced)) if voiced else None
    limit = min(6., max(0., float(max_db)))
    gains = []
    result = []
    for (offset, audio), level in zip(entries, levels):
        correction = 0.
        if enabled and target is not None and level is not None and abs(target - level) > 2:
            correction = min(limit, max(-limit, (target - level) / 2))
        gains.append(round(correction, 2))
        result.append((offset, audio if not correction else audio * 10 ** (correction / 20)))
    return result, levels, gains
