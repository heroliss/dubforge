"""人工播放时间覆盖：独立于源校对稿、声音参数及音频版本。"""
import hashlib
import json
import math

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.line_settings import identity


KEY = 'playback_timing_edits'


def records(project):
    """坏记录不能使整张列表不可读；具体失效情况由 resolve 展示。"""
    if not hasattr(project, 'params'):
        return {}
    values = crud.project_params(project).get(KEY, {})
    return values if isinstance(values, dict) else {}


def fingerprint(project):
    """仅影响播放产物，调用者不能把它并入模型音频的输入身份。"""
    return records(project)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def revision(project, line, *, saved=None, audio_version=None):
    values = records(project) if saved is None else saved
    chosen = line.get('selection', {})
    selection = [chosen.get('source', line.get('source', 'dub')),
                 chosen.get('take', line.get('take'))]
    payload = [identity(line), line['start'], line['end'], line.get('edit_revision'),
               selection, audio_version, values.get(str(line['id']))]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def resolve(project, line, *, saved=None, audio_version=None):
    """源句身份改变时只报告失效，不能悄悄将旧句手调套给新句。"""
    values = records(project) if saved is None else saved
    record = values.get(str(line['id']))
    valid = (isinstance(record, dict) and record.get('identity') == identity(line)
             and _number(record.get('start'))
             and (record.get('end') is None or
                  (_number(record['end']) and record['end'] > record['start'])))
    source = line.get('source', line.get('selection', {}).get('source', 'dub'))
    return {
        'active': bool(valid and source == 'dub'),
        'stale': bool(record is not None and not valid),
        'start': float(record['start']) if valid else None,
        'end': float(record['end']) if valid and record.get('end') is not None else None,
        'revision': revision(project, line, saved=values, audio_version=audio_version),
    }


def save(project, line, body, *, seconds=None, video_duration=None, lines=(), audio_version=None):
    """在项目编辑锁内调用；只保存播放覆盖，不改写源时间或音频元数据。

    end 为可选保留窗口。窗口只能容纳完整音频，不能借结束时间裁音频；
    未指定时随当前所选完整音频变化。其他人工句必须保序且不重叠，自动
    句交由下一次局部排布避让；确实放不下时继续明确报告冲突。
    """
    if not isinstance(body, dict):
        raise DubForgeError('播放时间修改格式无效', code='PLAYBACK_TIMING_INVALID')
    saved = records(project)
    if body.get('revision') != revision(project, line, saved=saved, audio_version=audio_version):
        raise DubForgeError('本句或人工播放时间已变化，请重新打开后再保存',
                            code='PLAYBACK_TIMING_CONFLICT')
    key = str(line['id'])
    if body.get('reset') is True:
        result = None
    else:
        source = line.get('source', line.get('selection', {}).get('source', 'dub'))
        if source != 'dub' or not _number(seconds) or seconds <= 0:
            raise DubForgeError('请先选择有效配音，再调整播放时间；原声和静音沿用源时间',
                                code='PLAYBACK_TIMING_UNAVAILABLE')
        if set(body) - {'revision', 'start', 'end'}:
            raise DubForgeError('播放时间修改格式无效', code='PLAYBACK_TIMING_INVALID')
        start, end = body.get('start'), body.get('end')
        if not _number(start) or (end is not None and not _number(end)):
            raise DubForgeError('请输入大于或等于零的有效播放秒数', code='PLAYBACK_TIMING_INVALID')
        start = round(float(start), 3)
        end = round(float(end), 3) if end is not None else None
        audio_end = start + float(seconds)
        # 表单按毫秒输入，WAV 采样点可能多出不足半毫秒；最终播放
        # 仍使用完整采样时长，不能把显示舍入当成需要重新合成。
        if end is not None and end < audio_end - .0005:
            raise DubForgeError(f'结束时间至少为 {audio_end:.3f} 秒，以保留整句声音；缩短声音请调整配音参数后重配',
                                code='PLAYBACK_TIMING_INVALID')
        effective_end = max(audio_end, end if end is not None else audio_end)
        if (_number(video_duration) and video_duration > 0
                and effective_end > video_duration + 1e-7):
            raise DubForgeError('人工播放范围超出视频结尾，请向前移动或缩短配音后再试',
                                code='PLAYBACK_TIMING_OUTSIDE_VIDEO')
        for other in lines:
            if other['id'] == line['id']:
                continue
            manual = resolve(project, other, saved=saved)
            if not manual['active']:
                continue
            duration = other.get('seconds', other.get('wav', {}).get('seconds'))
            if not _number(duration) or duration <= 0:
                continue
            other_start = manual['start']
            other_end = max(other_start + duration, manual['end'] or 0)
            # 比较源句顺序，不能用已滑动的播放位置替换源身份。
            source_start = (other.get('voice_timing') or [other['start']])[0]
            line_start = (line.get('voice_timing') or [line['start']])[0]
            is_before = (source_start, other['id']) < (line_start, line['id'])
            if (is_before and other_end > start + 1e-7) or (
                    not is_before and effective_end > other_start + 1e-7):
                raise DubForgeError(f"人工播放范围与第 {other['id']} 句重叠或顺序颠倒，请先调整相邻人工句",
                                    code='PLAYBACK_TIMING_OVERLAP')
        result = {'identity': identity(line), 'start': start}
        if end is not None:
            result['end'] = end
    params = crud.project_params(project)
    values = params.setdefault(KEY, {})
    if not isinstance(values, dict):
        values = params[KEY] = {}
    if result is None:
        values.pop(key, None)
    else:
        values[key] = result
    if not values:
        params.pop(KEY, None)
    project.params = json.dumps(params, ensure_ascii=False)
    crud.save_project(project)
    crud.write_manifest(project.uid)
    return result
