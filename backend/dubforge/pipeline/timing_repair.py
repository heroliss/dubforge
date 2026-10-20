"""复用配音的有限缩时修复：复现实际声音快照，不把当前项目新音色套回旧槽。"""
import copy
import json
import math

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_edit, line_settings, tts_duration


class TimingRepairError(DubForgeError):
    code = 'TTS_TIMING_REPAIR_UNAVAILABLE'
    suggestion = '请在字幕与配音页重新生成此句，或调整播放时间、译文与缩短重试设置后继续。'


_RUNTIME_KEYS = {'url', 'project_dir', 'request_timeout_seconds', 'duration_retry_factors'}
_FLOAT_FIELDS = {'emo_weight', 'top_p', 'temperature', 'length_penalty', 'repetition_penalty'}
_INT_FIELDS = {'top_k', 'num_beams', 'max_mel_tokens', 'max_text_tokens_per_segment'}


def _factor(value):
    return type(value) in (int, float) and math.isfinite(value) and .5 <= value <= 2


def _validate_snapshot(snapshot):
    """缺失字段不能用当前项目值猜测，也不能以 None 覆盖后端默认值。"""
    invalid = [key for key in sorted(_FLOAT_FIELDS)
               if type(snapshot.get(key)) not in (int, float) or not math.isfinite(snapshot[key])]
    invalid.extend(key for key in sorted(_INT_FIELDS)
                   if type(snapshot.get(key)) is not int or snapshot[key] < (0 if key == 'top_k' else 1))
    if type(snapshot.get('do_sample')) is not bool:
        invalid.append('do_sample')
    if not isinstance(snapshot.get('lang'), str) or not snapshot['lang'].strip():
        invalid.append('lang')
    mode = snapshot.get('emo_mode')
    if mode not in {'same', 'preset', 'stable', 'dynamic', 'vector', 'text'}:
        invalid.append('emo_mode')
    for part in ('timbre', 'emo'):
        if snapshot.get(f'{part}_mode') == 'preset' and not (
                isinstance(snapshot.get(f'{part}_audio'), str) and snapshot[f'{part}_audio']):
            invalid.append(f'{part}_audio')
    if any(snapshot.get(f'{part}_mode') in {'dynamic', 'stable'} for part in ('timbre', 'emo')):
        cap = snapshot.get('dynamic_reference_max_seconds')
        if type(cap) not in (int, float) or not math.isfinite(cap) or cap <= 0:
            invalid.append('dynamic_reference_max_seconds')
    if mode == 'vector':
        vector = snapshot.get('emo_vector')
        if (not isinstance(vector, list) or len(vector) != 8
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)):
            invalid.append('emo_vector')
    if mode == 'text' and not isinstance(snapshot.get('emo_text'), str):
        invalid.append('emo_text')
    if 'emo_random' in snapshot and type(snapshot['emo_random']) is not bool:
        invalid.append('emo_random')
    if invalid:
        raise TimingRepairError('当前配音的声音快照不完整或无效（' + '、'.join(invalid)
                                + '），不能用当前设置补齐后自动缩时；请手动重新生成此句。')


def context(project, line, root, take, base_cfg):
    """只读构建本句合成上下文；历史声音参数与当前连接/运行保护分开。"""
    if base_cfg('tts.backend', 'indextts25') != 'indextts25':
        raise TimingRepairError('当前配音引擎不是 IndexTTS，不能在自动修复时切换引擎；请先选择 IndexTTS 或手动重做此句。')
    if not dubbing_edit.snapshot_current(root, line, take, project=project, cfg_get=base_cfg):
        raise TimingRepairError('当前配音缺少可核验的生成快照，不能自动改变其声音。')
    record = dubbing_edit.read_take(root, line, take)
    snapshot = record.get('settings')
    if (not isinstance(snapshot, dict) or snapshot.get('timbre_mode') not in {'preset', 'stable', 'dynamic'}
            or not _factor(snapshot.get('duration_factor'))):
        raise TimingRepairError('当前配音没有可复现的 IndexTTS 声音参数，不能自动缩时。')
    _validate_snapshot(snapshot)
    from dubforge.pipeline.step_tts import REFERENCE_SNAPSHOT_KEY, LEGACY_REFERENCE_KEY, _reference_fingerprint
    references = snapshot.get(REFERENCE_SNAPSHOT_KEY, {})
    if not isinstance(references, dict):
        raise TimingRepairError('当前配音的原声参考快照无效，请手动重新生成此句。')
    for part in ('timbre', 'emo'):
        reference = references.get(part)
        if reference is not None and not isinstance(reference, dict):
            raise TimingRepairError('此配音的参考片段快照无效，请手动重新生成此句。')
        if snapshot.get(f'{part}_mode') == 'stable' and reference is None:
            raise TimingRepairError('此配音使用旧版开头原声，缺少实际参考片段快照；请按指定参考句手动重做后再自动缩时。')
        if reference is None or reference.get('mode') == 'preset':
            continue
        start, duration = reference.get('start'), reference.get('duration')
        if type(start) not in (int, float) or type(duration) not in (int, float):
            raise TimingRepairError('此配音的原声参考时间快照不完整，请手动重新生成此句。')
        try:
            version = _reference_fingerprint(root / 'audio/vocal.wav', start, duration)
        except DubForgeError as exc:
            raise TimingRepairError('此配音的原声参考不可用，请手动重新生成此句。') from exc
        if version != reference.get('source_version'):
            raise TimingRepairError('此配音的原声参考文件已变化，不能自动换参考缩时；请手动重新生成此句。')
    from dubforge.pipeline import pronunciation
    # 只沿用该声音实际生成时的读音，不能借缩时换成当前新注音。
    if pronunciation.KEY in snapshot:
        pronunciation.validate(snapshot[pronunciation.KEY], line['text'])
    retry = record.get('duration_retry') or {}
    used = retry.get('used_factor', snapshot['duration_factor'])
    if not _factor(used):
        raise TimingRepairError('当前配音的实际时长因子无效，不能可靠地继续缩时。')
    current = line_settings.effective_config(project, line, base_cfg)
    settings = copy.deepcopy(snapshot)
    spoken = dubbing_edit.recorded_reading(project, line, record)
    if spoken is not None and spoken != line['text']:
        settings[pronunciation.KEY] = spoken
    if any(snapshot.get(f'{part}_mode') == 'dynamic' and part not in references for part in ('timbre', 'emo')):
        # 旧 dynamic 仍可自动修复，但要明确重放历史解析规则，不能偷换成新默认参考句。
        settings[LEGACY_REFERENCE_KEY] = True
    settings['duration_factor'] = float(used)
    # 新增的空情感参数与旧实现含义一致，其余声音参数只认生成快照。
    settings.setdefault('emo_text', '')
    settings.setdefault('emo_random', False)
    for key in _RUNTIME_KEYS:
        settings[key] = current(f'tts.indextts25.{key}', settings.get(key))
    params = copy.deepcopy(crud.project_params(project))
    params.pop('tts_lines', None)
    synthetic = project.model_copy(update={'params': json.dumps(params, ensure_ascii=False)})

    def get(key, default=None):
        if key == 'tts.backend':
            return 'indextts25'
        if key == 'tts.indextts25':
            return copy.deepcopy(settings)
        if key.startswith('tts.indextts25.'):
            return copy.deepcopy(settings.get(key.removeprefix('tts.indextts25.'), default))
        if key == 'tts':
            value = copy.deepcopy(base_cfg(key, default) or {})
            value.update(backend='indextts25', indextts25=copy.deepcopy(settings))
            return value
        return copy.deepcopy(base_cfg(key, default))

    return synthetic, get


def remaining_factors(record, cfg):
    """实际已采用的因子同样是下限；只重试尚未成功返回的更小因子。"""
    retry = record.get('duration_retry') or {}
    used = retry.get('used_factor', cfg('tts.indextts25.duration_factor', 1.))
    attempted = [item['factor'] for item in retry.get('attempts', [])
                 if isinstance(item, dict) and _factor(item.get('factor'))]
    actual = record.get('settings', {}).get('duration_factor')
    floor = min([float(used), *([float(actual)] if _factor(actual) else []), *map(float, attempted)])
    return [factor for factor in tts_duration.attempt_factors(used, cfg(tts_duration.KEY, ''))
            if factor < floor - 1e-9]


def inspect(deps, lines=None, exclude_ids=()):
    """只读检查可自动修复的旧配音；人工位置、声源和逐句设置不被改写。"""
    from dubforge.pipeline import voice_timing
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.pipeline.dubbing_policy import _requirements
    lines = list(lines if lines is not None else deps.production_lines())
    inputs = [{**row, 'source': row['source'] if row['source'] == 'original'
               else 'dub' if row.get('take') is not None else 'silent'} for row in lines]
    layout = voice_timing.plan(deps.project, deps.root, inputs, deps.cfg, dependencies=deps)
    result = dict(repair_rows=[], unresolved_ids=list(layout['timing']['unresolved_ids']),
                  blocked=[], requirements=[], timing=layout['timing'])
    if not layout['timing']['enabled'] and not layout['timing'].get('manual'):
        return result
    by_id = {line['id']: line for line in lines}
    excluded = set(exclude_ids)
    for timing in layout['timing']['lines']:
        id = timing['id']
        if id in excluded or id not in result['unresolved_ids']:
            continue
        line = by_id[id]
        if line['source'] != 'dub' or line.get('take') is None:
            continue
        if timing.get('manual_conflict') or not timing.get('needs_scale'):
            result['blocked'].append(dict(id=id, reason='人工播放起点导致顺序或间隔冲突，请移动起点或恢复自动排布。'))
            continue
        try:
            synthetic, get = context(deps.project, line, deps.root, line['take'], deps.cfg)
            record = dubbing_edit.read_take(deps.root, line, line['take'])
            factors = remaining_factors(record, get)
            if not factors:
                raise TimingRepairError('已没有尚未尝试的更小时长因子，请调整缩短重试因子或本句内容。')
            snapshot_deps = Dependencies(synthetic, settings=deps.settings, overrides=deps.overrides)
            snapshot_deps.cfg = get
            requirements = _requirements(snapshot_deps, [line])
        except DubForgeError as exc:
            result['blocked'].append(dict(id=id, reason=exc.message))
            continue
        result['repair_rows'].append(dict(id=id, take=line['take'], identity=line_settings.identity(line),
            kind='repair', status='queued', force=False,
            source_before=line['source'], take_before=line['take'],
            source_version=dubbing_edit.file_version(dubbing_edit.take_path(deps.root, id, line['take'])),
            used_factor=get('tts.indextts25.duration_factor'), repair_factors=factors))
        result['requirements'].extend(requirements)
    unique = {(item.get('step'), item.get('name'), item.get('reason')): item for item in result['requirements']}
    result['requirements'] = list(unique.values())
    return result
