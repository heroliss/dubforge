"""从当前选用音频派生统一播放时钟；预览、字幕和成片共用，不改写源台词。"""
import math
import copy
import hashlib
import json
import struct
from functools import lru_cache
from pathlib import Path

from dubforge.errors import ConfigError, DubForgeError
from dubforge.pipeline import dubbing_edit, playback_timing
from dubforge.pipeline.dubbing_placement import allocate_voice_windows, build_plan
from dubforge.pipeline.tts_duration import audio_seconds


STRATEGY_VERSION = 'natural-first-bounded-manual-v4'


def _video_input(project, pdir):
    from dubforge.pipeline.video_source import resolve_video
    path = resolve_video(project, Path(pdir))
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return path, stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=128)
def _probe_video(path, modified_ns, size):
    # 共享成片的有界探测实现；文件版本改变才重新探测，不把昂贵进程放进轮询。
    from dubforge.pipeline.step_assemble import _ffprobe_duration
    return _ffprobe_duration(Path(path)) or None


def video_seconds(project, pdir):
    """视频末尾以真实媒体为准；按文件版本缓存，坏媒体不会反复启动探测进程。"""
    value = _video_input(project, pdir)
    return _probe_video(str(value[0]), value[1], value[2]) if value else None


def _wav_duration_identity(path, actual_size):
    """只核验有界 PCM WAV 容器头；遇到 data 即停，绝不读取音频载荷。"""
    limit = 4096
    # 禁用文件缓冲的预读，否则 read(12) 也可能提前读入后面的 PCM 数据。
    with path.open('rb', buffering=0) as stream:
        container = stream.read(12)
        if len(container) != 12 or container[:4] != b'RIFF' or container[8:] != b'WAVE':
            return None
        end = struct.unpack_from('<I', container, 4)[0] + 8
        if end > actual_size or end < 12:
            return None
        position, chunks, frame_size = 12, [], None
        while position + 8 <= min(end, limit):
            header = stream.read(8)
            if len(header) != 8:
                return None
            kind, size = header[:4], struct.unpack_from('<I', header, 4)[0]
            body = position + 8
            if body + size > end:
                return None
            if kind == b'data':
                if frame_size is None or size % frame_size or body + size + size % 2 > end:
                    return None
                return container.hex(), tuple(chunks), header.hex(), body, actual_size
            following = body + size + size % 2
            if following > min(end, limit):
                return None
            if kind == b'fmt ':
                # 扩展、压缩或额外时长表交给真实读取器，不能猜它们的缓存身份。
                if frame_size is not None or size not in (16, 18):
                    return None
                content = stream.read(size)
                if len(content) != size:
                    return None
                tag, channels, rate, byte_rate, alignment, bits = struct.unpack_from('<HHIIHH', content)
                if (tag != 1 or not channels or not rate or bits not in (8, 16, 24, 32)
                        or alignment != channels * (bits // 8) or byte_rate != rate * alignment
                        or (size == 18 and content[16:] != b'\0\0')):
                    return None
                frame_size = alignment
                chunks.append((position, header.hex(), content.hex()))
                stream.seek(size % 2, 1)
            elif kind in (b'JUNK', b'PAD ', b'LIST'):
                chunks.append((position, header.hex()))
                stream.seek(size + size % 2, 1)
            else:
                return None
            position = following
    return None


def _audio_read_identity(path):
    try:
        path.stat()
    except FileNotFoundError:
        return ('missing',)
    except OSError:
        return None
    try:
        version = dubbing_edit.file_read_identity(path)
        header = _wav_duration_identity(path, version[0][0])
    except OSError:
        # 已存在的文件在读身份/头时消失，不能登记成稳定的缺失状态。
        return None
    return (version, header) if header is not None else None


def _read_identity(root, rows, *, snapshots=None, dependencies=None):
    """小型记录核验内容、音频只核验时长容器头；无法证明时不复用缓存。"""
    versions = []
    for row in rows:
        take = row.get('take')
        if row.get('source', 'dub') != 'dub' or take is None:
            continue
        path = dubbing_edit.take_path(root, row['id'], take)
        audio = _audio_read_identity(path)
        if audio is None:
            return None
        versions.append(audio)
        version, snapshot = dubbing_edit.take_snapshot_identity(root, row, take, dependencies=dependencies)
        if version is None:
            return None
        versions.append(version)
        if snapshots is not None:
            snapshots[(row['id'], take)] = snapshot
    return versions


def _cache_key(kind, project, root, rows, options, extra=None, *, snapshots=None, dependencies=None):
    versions = _read_identity(root, rows, snapshots=snapshots, dependencies=dependencies)
    if versions is None:
        return None
    value = [kind, str(root), project.params, rows, options, extra, versions]
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def fingerprint(deps):
    """只读排布输入摘要；不调用 ffprobe，不要求字幕步骤硬依赖配音完成。"""
    options = settings(deps.cfg)
    result = {'strategy': STRATEGY_VERSION, 'settings': options}
    manual = playback_timing.fingerprint(deps.project)
    if manual:
        result['manual'] = manual
    if not options['enabled'] and not manual:
        return result
    root = Path(getattr(deps, 'pdir', None) or deps.root)
    video = _video_input(deps.project, root)
    result['video'] = ([str(video[0]), video[1], video[2]] if video else None)
    rows = deps.production_lines()
    cache = getattr(deps, '_voice_timing_cache', None)
    snapshots = {}
    key = _cache_key('fingerprint', deps.project, root, rows,
                     [options, deps.cfg('tts')], result['video'],
                     snapshots=snapshots, dependencies=deps) if cache is not None else None
    if cache is not None and key is not None and key in cache:
        return copy.deepcopy(cache[key])
    result['voices'] = []
    for row in rows:
        source, take = row['source'], row.get('take')
        valid = (source == 'dub' and take is not None
                 and dubbing_edit.playable(deps.project, row, root, take, dependencies=deps,
                                          snapshot=snapshots.get((row['id'], take))))
        result['voices'].append({
            'id': row['id'], 'start': row['start'], 'end': row['end'],
            'source': source, 'take': take,
            'version': dubbing_edit.file_version(dubbing_edit.take_path(
                root, row['id'], take)) if valid else None,
        })
    if cache is not None and key is not None:
        cache[key] = copy.deepcopy(result)
    return result


def settings(cfg):
    """排布是制作策略，独立于模型合成参数和逐句音频身份。"""
    values = {
        'enabled': cfg('tts.placement.enabled', True),
        'max_shift_seconds': cfg('tts.placement.max_shift_seconds', 2.0),
        'window_lines': cfg('tts.placement.window_lines', 9),
        'inter_sentence_gap': cfg('tts.placement.inter_sentence_gap', .08),
    }
    if type(values['enabled']) is not bool:
        raise ConfigError('请为自适应配音排布选择开启或关闭。')
    if type(values['window_lines']) is not int or not 3 <= values['window_lines'] <= 15:
        raise ConfigError('配音缩时分析窗口必须是 3～15 句的整数。')
    for key, label in (('max_shift_seconds', '配音最大滑动范围'),
                       ('inter_sentence_gap', '配音句间安全间隔')):
        value = values[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ConfigError(f'{label}必须是大于或等于零的有效秒数。')
        values[key] = float(value)
    return values


def _duration(row, root, *, project=None, cfg=None, dependencies=None, snapshot=None):
    """调用方已核验的时长优先；明确无效的历史文件绝不能重新变成可试听音频。"""
    source = row.get('source', 'dub')
    if source == 'original':
        return max(0.0, float(row['end']) - float(row['start'])), None
    if source == 'silent':
        return 0.0, None
    unavailable = row.get('exists') is False or (
        isinstance(row.get('wav'), dict) and row['wav'].get('exists') is False)
    if unavailable:
        return None, 'audio_unavailable'
    for key in ('seconds', 'audio_duration'):
        value = row.get(key)
        if value is None:
            continue
        if type(value) in (int, float) and math.isfinite(value) and value > 0:
            return float(value), None
        return None, 'audio_invalid'
    take = row.get('take')
    if take is None:
        return None, 'audio_missing'
    path = dubbing_edit.take_path(root, row['id'], take)
    if not path.is_file():
        return None, 'audio_missing'
    recorded = (dubbing_edit.take_snapshot(root, row, take, dependencies=dependencies)
                if snapshot is None else snapshot)
    if recorded and not dubbing_edit.snapshot_current(root, row, take, snapshot=recorded,
                                                     project=project, cfg_get=cfg, dependencies=dependencies):
        return None, 'audio_unavailable'
    try:
        return audio_seconds(path), None
    except DubForgeError:
        return None, 'audio_invalid'


def plan(project, pdir, rows, cfg, video_duration=None, *, dependencies=None):
    """同一读取中的成片版本共用相同排布；输入或音频变化会重算。"""
    cache = getattr(dependencies, '_voice_timing_cache', None)
    if cache is None:
        return _build_plan(project, pdir, rows, cfg, video_duration)
    rows = list(rows)
    root = Path(pdir)
    if video_duration is None:
        video_duration = video_seconds(project, root)
    version = getattr(dependencies, 'version', None)
    snapshots = {}
    key = _cache_key('plan', project, root, rows, [settings(cfg), cfg('tts')],
                     [video_duration, version.get('values') if version else None],
                     snapshots=snapshots, dependencies=dependencies)
    if key is None:
        return _build_plan(project, root, rows, cfg, video_duration, dependencies=dependencies, snapshots=snapshots)
    if key not in cache:
        cache[key] = _build_plan(project, root, rows, cfg, video_duration,
                                 dependencies=dependencies, snapshots=snapshots)
    return copy.deepcopy(cache[key])


def _build_plan(project, pdir, rows, cfg, video_duration=None, *, dependencies=None, snapshots=None):
    """读取选用音频后计算时钟；不写产物、不读取旧铺轨文件、不调用模型。

    ``inputs`` 保留完整音频时长，只有可行局部窗口才应用新起点；一个窗口
    放不下时保留该窗口原起点并报告缩时需求，不撤回其他可行窗口。所有
    源字段和原始输入不变，``timing.lines`` 同时保留建议范围与实际采用范围。
    """
    options = settings(cfg)
    root = Path(pdir)
    if video_duration is None:
        video_duration = video_seconds(project, root)
    prepared, versions, diagnostics = [], {}, []
    seen = set()
    for raw in rows:
        row = dict(raw)
        if row['id'] in seen:
            raise DubForgeError('配音台词编号重复，无法计算统一排布。', code='DUB_TIMELINE_INVALID')
        seen.add(row['id'])
        row.setdefault('source', 'dub')
        seconds, error = _duration(row, root, project=project, cfg=cfg, dependencies=dependencies,
                                   snapshot=(snapshots or {}).get((row['id'], row.get('take'))))
        row['seconds'] = seconds
        prepared.append(row)
        take = row.get('take')
        versions[row['id']] = (dubbing_edit.file_version(
            dubbing_edit.take_path(root, row['id'], take)) if take is not None else None)
        if error:
            diagnostics.append({'id': row['id'], 'kind': error,
                                'message': '所选配音尚未生成或不可用，保留源时间并等待有效音频。'})

    def allocate(values):
        return allocate_voice_windows(
            values, max_shift_seconds=options['max_shift_seconds'],
            window_lines=options['window_lines'],
            inter_sentence_gap=options['inter_sentence_gap'], video_duration=video_duration)

    # 纯自动结果可与人工最终值并排查看；人工记录只作为独立固定障碍，
    # 不反向改源稿或模型音频身份。无人工记录时只计算一次。
    automatic = allocate(prepared) if options['enabled'] else []
    automatic_by_id = {row['id']: row for row in automatic}
    saved = playback_timing.records(project)
    manual = {row['id']: playback_timing.resolve(
        project, row, saved=saved, audio_version=versions[row['id']]) for row in prepared}
    active = {row['id'] for row in prepared if manual[row['id']]['active']
              and row['source'] == 'dub' and row['seconds'] is not None}
    constrained = [{**row, **({'fixed_start': manual[row['id']]['start'],
                               'fixed_end': manual[row['id']]['end']}
                              if row['id'] in active else {})} for row in prepared]
    if options['enabled']:
        allocated = allocate(constrained) if active else automatic
    else:
        allocated = []
        for row in prepared:
            change = manual[row['id']]
            if row['id'] not in active:
                continue
            end = change['end'] if change['end'] is not None else change['start'] + row['seconds']
            capacity = max(0.0, min(end, video_duration) - change['start']) if video_duration else end - change['start']
            needs_scale = capacity < row['seconds'] - .0005
            allocated.append({**row, 'allocated_start': change['start'],
                              'allocated_end': change['start'] + capacity,
                              'needs_scale': needs_scale, 'window_ready': not needs_scale,
                              'scale_factor': min(1.0, capacity / row['seconds'])})
    by_id = {row['id']: row for row in allocated}
    timing_rows, inputs = [], []
    for row in prepared:
        suggested = by_id.get(row['id'], {})
        auto = automatic_by_id.get(row['id'], {})
        change = manual[row['id']]
        is_manual = row['id'] in active
        needs_scale = bool(suggested.get('needs_scale'))
        usable = (options['enabled'] and row['source'] == 'dub'
                  and row['seconds'] is not None
                  and suggested.get('window_ready', not needs_scale))
        start, end = float(row['start']), float(row['end'])
        actual_start = (change['start'] if is_manual else
                        float(suggested['allocated_start']) if usable else row['start'])
        actual_end = (max(actual_start + row['seconds'], change['end'] or 0) if is_manual else
                      actual_start + row['seconds'] if usable else row['end'])
        auto_usable = (options['enabled'] and row['source'] == 'dub' and row['seconds'] is not None
                       and auto.get('window_ready', not auto.get('needs_scale')))
        auto_start = float(auto['allocated_start']) if auto_usable else row['start']
        auto_end = auto_start + row['seconds'] if auto_usable else row['end']
        entry = {
            'id': row['id'], 'source': row['source'], 'take': row.get('take'),
            'version': versions[row['id']],
            'source_start': start, 'source_end': end,
            'automatic_start': auto_start, 'automatic_end': auto_end,
            'automatic_ready': bool(auto_usable),
            'manual_start': change['start'], 'manual_end': change['end'],
            'manual_active': is_manual, 'manual_stale': change['stale'],
            'timing_revision': change['revision'],
            'effective_mode': 'manual' if is_manual else 'automatic' if usable else 'source',
            'allocated_start': suggested.get('allocated_start', start),
            'allocated_end': suggested.get('allocated_end', end),
            'seconds': row['seconds'], 'needs_scale': needs_scale,
            'scale_factor': suggested.get('scale_factor', 1.0),
            'applied': bool(usable or is_manual), 'applied_start': actual_start, 'applied_end': actual_end,
            'audio_end': actual_start + row['seconds'] if row['seconds'] is not None else None,
            **{key: suggested[key] for key in (
                'window_id', 'window_start', 'window_end', 'window_lines', 'window_ready',
                'available_start', 'available_end', 'available_seconds',
                'left_extension', 'right_extension') if key in suggested},
        }
        timing_rows.append(entry)
        value = {**row, 'start': actual_start, 'end': actual_end}
        if usable or is_manual:
            # 已有人工时移记录仍以最初源时间判定音频身份，不能被再次排布覆盖。
            value['voice_timing'] = list(row.get('voice_timing') or [row['start'], row['end']])
        inputs.append(value)

    placement = build_plan(inputs, video_duration)
    timing_by_id = {row['id']: row for row in timing_rows}
    if active:
        for issue in placement['issues']:
            involved = {issue['id'], issue.get('next_id')} & active
            if not involved or issue['kind'] not in {'overlap', 'video_overrun'}:
                continue
            # 切槽后完整音频可能增长，人工起点仍保留。明确报告而不能
            # 通过偷偷解除人工锁或截断音频来制造“无冲突”。
            # 两个人工起点之间相撞时，缩短前句才会释放容量；不能连带
            # 要求后句也加速。若前句为自动句，其窗口已有缩时建议。
            if issue['id'] in active:
                item = timing_by_id[issue['id']]
                item['needs_scale'] = True
                item['window_ready'] = False
                available = max(0.0, issue['available'] - (
                    options['inter_sentence_gap'] if issue['kind'] == 'overlap' else 0))
                item['allocated_end'] = min(item['allocated_end'], item['allocated_start'] + available)
                item['available_seconds'] = max(0.0, item['allocated_end'] - item['allocated_start'])
                item['scale_factor'] = min(item.get('scale_factor') or 1.0,
                                           item['available_seconds'] / item['seconds'])
            else:
                # 前方原声不可缩短；此时需要移动人工起点，不能假装
                # 压快后句就能解除它与先前原声的重叠。
                for line_id in involved:
                    timing_by_id[line_id]['manual_conflict'] = True
            diagnostics.append({'id': issue['id'], 'kind': 'manual_timing_conflict',
                                'message': '人工播放时间与完整声音冲突，请移动人工起点、恢复自动或缩短配音后重试。',
                                'next_id': issue.get('next_id')})

        # build_plan 按播放位置排序，只报告真实声音范围相撞。人工起点
        # 还必须保留源句顺序及安全间隔，不能让整句跨到前句以前却被判定可用。
        audible = [row for row in sorted(timing_rows, key=lambda row: (row['source_start'], row['id']))
                   if row['seconds'] is not None and row['seconds'] > 0]
        for previous, current in zip(audible, audible[1:]):
            involved = {previous['id'], current['id']} & active
            if not involved:
                continue
            available = current['applied_start'] - previous['applied_start'] - options['inter_sentence_gap']
            if previous['seconds'] <= available + 1e-7:
                continue
            if previous['id'] in active and available >= 0:
                previous['needs_scale'] = True
                previous['window_ready'] = False
                previous['allocated_end'] = min(previous['allocated_end'], previous['allocated_start'] + available)
                previous['available_seconds'] = max(0.0, previous['allocated_end'] - previous['allocated_start'])
                previous['scale_factor'] = min(previous.get('scale_factor') or 1.,
                                               previous['available_seconds'] / previous['seconds'])
            elif previous['source'] == 'original' or available < 0:
                for line_id in involved:
                    timing_by_id[line_id]['manual_conflict'] = True
            if not any(item['kind'] == 'manual_timing_conflict'
                       and item['id'] == previous['id'] and item.get('next_id') == current['id']
                       for item in diagnostics):
                diagnostics.append({'id': previous['id'], 'next_id': current['id'],
                                    'kind': 'manual_timing_conflict',
                                    'message': '人工播放起点使句子顺序或安全间隔冲突，请移动人工起点或恢复自动。'})

    unresolved_ids = [row['id'] for row in timing_rows if row['needs_scale'] or row.get('manual_conflict')]
    missing_ids = [item['id'] for item in diagnostics if item['kind'].startswith('audio_')]
    timing = {
        'enabled': options['enabled'], 'settings': options,
        'ready': not unresolved_ids and not missing_ids,
        'shifted': sum(row['applied'] and abs(row['applied_start'] - row['source_start']) > 1e-7
                       for row in timing_rows),
        'manual': len(active),
        'manual_stale': sum(row['manual_stale'] for row in timing_rows),
        'unresolved': len(unresolved_ids), 'unresolved_ids': unresolved_ids,
        'missing': len(missing_ids), 'missing_ids': missing_ids,
        'lines': timing_rows, 'diagnostics': diagnostics,
    }
    return {'inputs': inputs, 'placement': placement, 'timing': timing}


def apply(rows, derived):
    """把同一排布时钟用于任意语言字幕；编号和源时间匹配即可，不比较文案。"""
    timing = derived.get('timing', derived)
    by_id = {row['id']: row for row in timing.get('lines', [])}
    result = []
    for original in rows:
        row = dict(original)
        match = by_id.get(row['id'])
        if (match and match.get('applied')
                and abs(float(row['start']) - match['source_start']) <= 1e-7
                and abs(float(row['end']) - match['source_end']) <= 1e-7):
            row.update(start=match['applied_start'], end=match['applied_end'],
                       voice_timing=list(row.get('voice_timing') or [row['start'], row['end']]))
        result.append(row)
    return result
