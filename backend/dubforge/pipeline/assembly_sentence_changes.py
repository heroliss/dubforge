"""按需对照句子与成功成片；只记录真实渲染输入，不把未应用的TTS参数当成新声音。"""
import copy
import json
from dubforge.domain import crud
from dubforge.pipeline import dubbing_edit

SCHEMA = 1


def _round(value):
    return round(float(value), 6)


def _media(deps, step, name):
    """使用登记摘要和轻量文件版本；查询绝不重新扫描大媒体。"""
    art = deps.arts.get(f'{step}/{name}')
    path = deps.path(step, name)
    if not art or not path or not path.is_file():
        return dict(missing=True)
    stat = path.stat()
    if art.sha256 and stat.st_size == art.size:
        return dict(sha256=art.sha256, size=art.size)
    return dict(file_version=dubbing_edit.file_version(path))


def _range(events, fallback):
    active = [event for group in events for event in group if event['end'] > event['start']]
    return (dict(start=min(e['start'] for e in active), end=max(e['end'] for e in active))
            if active else dict(start=_round(fallback['start']), end=_round(fallback['end'])))


def _globals(deps, version):
    from dubforge.pipeline import assembly_versions as av, content_redaction
    values = av.active_values(version)
    data = dict(source=_media(deps, 'download', 'video.mp4'),
                dimensions={key: version[key] for key in ('dubbing', 'burn_subtitles', 'include_comments')},
                values=values, output_cuts=version.get('output_cuts'))
    if version.get('output_cuts'):
        from dubforge.pipeline.output_cuts import RENDER_VERSION
        data['output_cuts_renderer'] = RENDER_VERSION
    if av.requires_dubbing(version) and float(version['values'].get('assemble.bgm_volume', 0)) > 0:
        data['background'] = _media(deps, 'preprocess', 'background.wav')
    review = content_redaction.plan(deps.for_version(version))
    data['redaction'] = dict(ranges=review['ranges'], blocked_ids=review['blocked_ids'])
    data['audio_redaction'] = content_redaction.audio_fingerprint(deps.for_version(version))
    if version['include_comments']:
        from dubforge.comment_studio import render_fingerprint_plan
        data['comments'] = render_fingerprint_plan(content_redaction.safe_overlay(deps.for_version(version), version.get('comment_overlay')))
    return data


def capture(deps, version):
    """形成当前完整呈现的逐句合同；仅文件stat/音频头/现有排布，无媒体内容hash。"""
    from dubforge.pipeline.subtitle_document import read
    from dubforge.pipeline.version_render import subtitle_lines
    from dubforge.pipeline.output_cuts import source_context
    rows = read(deps, translation=version['dubbing'] or version['burn_subtitles'] and version['values']['assemble.subtitle_track'] != 'source')
    lines = {str(row['id']): dict(id=row['id'], subtitle=[], audio=[], known=True,
             document={key: row[key] for key in ('source_text', 'text', 'start', 'end')},
             source_range=dict(start=_round(row['start']), end=_round(row['end']))) for row in rows}
    scoped = deps.for_version(version)
    if version['burn_subtitles']:
        for event in subtitle_lines(scoped, version):
            item = lines.get(str(event.index))
            if item is not None:
                item['subtitle'].append(dict(start=_round(event.start), end=_round(event.end), text=event.text))
    duration = source_context(deps)['duration'] or max((row['end'] for row in rows), default=0)
    if version['dubbing']:
        from dubforge.pipeline import voice_timing, audio_post
        from dubforge.pipeline.dubbing_placement import build_plan, region_entries
        selected = dubbing_edit.production_lines(deps.project, rows, deps.root, dependencies=deps)
        selected = [{**row, 'source': row['source'] if row['source'] == 'original' or row.get('take') is not None else 'silent'}
                    for row in selected]
        layout = voice_timing.plan(deps.project, deps.root, selected, scoped.cfg, video_duration=duration)
        by_id = {row['id']: row for row in selected}
        gains = version.get('audio_line_gains')
        for event in region_entries(build_plan(layout['inputs'], duration), 0, duration):
            row = by_id[event['id']]
            if row['source'] == 'silent':
                continue
            step, name = (('preprocess', 'vocal.wav') if row['source'] == 'original'
                          else ('tts', dubbing_edit.take_name(row['id'], row['take'])))
            path = deps.path(step, name) or (dubbing_edit.take_path(deps.root, row['id'], row['take']) if step == 'tts' else deps.root / 'audio/vocal.wav')
            gain = audio_post.line_state(deps.project, row, audio_version=dubbing_edit.file_version(path), saved=gains)['gain_db']
            lines[str(row['id'])]['audio'].append(dict(start=_round(event['clip_start']), end=_round(event['clip_end']),
                source=row['source'], identity=_media(deps, step, name), audio_offset=_round(event.get('audio_offset', 0)), gain_db=gain))
    for item in lines.values():
        item['range'] = _range([item['subtitle'], item['audio']], item['source_range'])
    global_inputs = _globals(deps, version)
    if version['dubbing'] and (version['values'].get('assemble.sentence_leveling', False)
            or version['values'].get('assemble.loudnorm', False)
            or version['values'].get('assemble.final_loudness_mode', 'off') != 'off'):
        # 均衡/整轨归一目标取决于整组声音；新WAV可能改变其他句增益。只存轻量
        # 输入身份，不在比较请求中扫描PCM或猜测均衡后的实际响度。
        global_inputs['audio_mix_inputs'] = {key: row['audio'] for key, row in lines.items()}
    from dubforge.pipeline.assembly_change_groups import comment_inputs
    from dubforge.pipeline.content_redaction import safe_overlay
    # 指纹摘要有意去掉人工来源；实际文字校验仍须使用真实方案，避免把允许
    # 完整显示的人工长评误当自动候选，再次套用候选长度建议。
    overlay = safe_overlay(scoped, version.get('comment_overlay')) if version['include_comments'] else None
    comments = comment_inputs(overlay, duration)
    return dict(schema=SCHEMA, coverage='known', duration=_round(duration), global_inputs=global_inputs,
                lines=lines, comment_inputs=comments)


def _clip(events, ranges, *, inside):
    result = []
    for event in events:
        spans = [(event['start'], event['end'])]
        if inside:
            spans = [(max(event['start'], r['start']), min(event['end'], r['end'])) for r in ranges]
        else:
            for r in ranges:
                spans = [piece for start, end in spans for piece in
                         ((start, min(end, r['start'])), (max(start, r['end']), end)) if piece[1] > piece[0]]
        for start, end in spans:
            if end <= start:
                continue
            item = {**copy.deepcopy(event), 'start': _round(start), 'end': _round(end)}
            if 'audio_offset' in item:
                item['audio_offset'] = _round(event['audio_offset'] + start - event['start'])
            result.append(item)
    return result


def _normalize(events):
    result = []
    for event in sorted(events, key=lambda item: (item['start'], item['end'])):
        if result:
            last = result[-1]
            keys = set(event) - {'start', 'end', 'audio_offset'}
            same = all(last.get(k) == event.get(k) for k in keys)
            continuous = ('audio_offset' not in event or abs(last['audio_offset'] + last['end'] - last['start'] - event['audio_offset']) < 1e-5)
            if same and continuous and abs(last['end'] - event['start']) < 1e-5:
                last['end'] = event['end']
                continue
        result.append(copy.deepcopy(event))
    return result


def _valid(snapshot):
    return (isinstance(snapshot, dict) and snapshot.get('schema') == SCHEMA
            and isinstance(snapshot.get('lines'), dict) and isinstance(snapshot.get('global_inputs'), dict))


def completed(deps, version):
    """局部成片只覆盖实际区间，其余继承基准证据；旧基准未知范围继续未知。"""
    current = capture(deps, {k: v for k, v in version.items() if k not in {'partial', 'partial_plan'}})
    if not version.get('partial'):
        return current
    from dubforge.pipeline.assembly_partial import load_plan
    plan = load_plan(deps, version)
    baseline = plan['base_version'].get('sentence_inputs')
    known = _valid(baseline)
    before = baseline['lines'] if known else {}
    result = copy.deepcopy(current)
    result['known_ranges'] = copy.deepcopy(plan['ranges'])
    full = (len(plan['ranges']) == 1 and plan['ranges'][0]['start'] <= 0
            and plan['ranges'][0]['end'] >= current['duration'] and plan['replace_audio'])
    if not known and not full:
        result['coverage'] = 'partial'
    for key in set(before) | set(current['lines']):
        old = before.get(key, dict(id=int(key), subtitle=[], audio=[], known=known))
        new = current['lines'].get(key, dict(id=int(key), subtitle=[], audio=[], known=True))
        target = result['lines'].setdefault(key, copy.deepcopy(old))
        for part in ('subtitle', 'audio'):
            if part == 'audio' and not plan['replace_audio']:
                target[part] = copy.deepcopy(old.get(part, []))
            else:
                target[part] = _normalize(_clip(old.get(part, []), plan['ranges'], inside=False)
                    + _clip(new.get(part, []), plan['ranges'], inside=True))
        target['known'] = bool(full or known and old.get('known', True))
        target['range'] = _range([target['subtitle'], target['audio']], target.get('source_range', new.get('source_range', dict(start=0, end=0))))
        shown = _normalize(target['subtitle'])
        # 文档只可对应实际采用的完整字幕事件；范围外沿用旧文档，同一句
        # 混合了新旧字幕时不伪造单一的历史原文/译文。
        target['document'] = (copy.deepcopy(old.get('document'))
            if target['known'] and shown == _normalize(old.get('subtitle', [])) else
            copy.deepcopy(new.get('document')) if target['known'] and shown == _normalize(new.get('subtitle', [])) else None)
    from dubforge.pipeline.assembly_change_groups import merged_comments, global_scope
    result['comment_inputs'] = merged_comments(plan['base_version'], current['comment_inputs'],
        plan['ranges'], full=full or len(plan['ranges']) == 1 and plan['ranges'][0]['start'] <= 0
            and plan['ranges'][0]['end'] >= current['duration'])
    if known:
        if not plan['replace_audio']:
            # 保留整条基准音轨时，不把当前未应用的混音/响度/背景设置
            # 伪装成实际成片输入，也不因比较键被省略而误判混合呈现。
            from dubforge.pipeline.assembly_versions import RENDER_KEYS
            from dubforge.pipeline.video_encoding import ENCODING_KEYS
            sound = {key for key in RENDER_KEYS if key.startswith('assemble.') and not key.startswith('assemble.subtitle_') and key not in ENCODING_KEYS}
            for key in sound:
                if key in baseline['global_inputs']['values']:
                    result['global_inputs']['values'][key] = baseline['global_inputs']['values'][key]
                else:
                    result['global_inputs']['values'].pop(key, None)
            if 'background' in baseline['global_inputs']:
                result['global_inputs']['background'] = copy.deepcopy(baseline['global_inputs']['background'])
            else:
                result['global_inputs'].pop('background', None)
            result['global_inputs']['audio_redaction'] = copy.deepcopy(baseline['global_inputs'].get('audio_redaction'))
            if 'audio_mix_inputs' in baseline['global_inputs']:
                result['global_inputs']['audio_mix_inputs'] = copy.deepcopy(baseline['global_inputs']['audio_mix_inputs'])
            else:
                result['global_inputs'].pop('audio_mix_inputs', None)
        result['mixed_globals'] = bool(baseline.get('mixed_globals')
            or global_scope(baseline['global_inputs']) != global_scope(result['global_inputs']))
        if result['mixed_globals']:
            result['coverage'] = 'partial'
    if any(not row['known'] for row in result['lines'].values()):
        result['coverage'] = 'partial'
    return result


def _dubbing_pending(deps, version, rows):
    if not version['dubbing']:
        return {}
    from dubforge.pipeline import dubbing_changes
    from dubforge.pipeline.step_tts import line_fingerprint
    result = {}
    for row in dubbing_edit.production_lines(deps.project, rows, deps.root, dependencies=deps):
        take = row.get('take')
        if row['source'] != 'dub' or take is None:
            continue
        path = dubbing_edit.take_path(deps.root, row['id'], take)
        data = dubbing_edit.read_take(deps.root, row, take)
        valid = dubbing_edit.playable(deps.project, row, deps.root, take, dependencies=deps, snapshot=data)
        value = dubbing_changes.view(deps.project, row, deps.root, take, dependencies=deps, snapshot=data,
            version=dubbing_edit.file_version(path), valid=valid,
            current_fingerprint=line_fingerprint(deps.project, row, dependencies=deps))
        if value['changes_pending']:
            result[row['id']] = dict(reasons=value['change_reasons'], blocking=not valid)
        elif not valid:
            result[row['id']] = dict(reasons=['所选配音尚未生成或不可用'], blocking=True)
    return result


def _manual_candidates(project, rows, coverage):
    if coverage == 'known':
        return []
    from dubforge.pipeline.subtitle_document import records
    edits = records(project)
    return [dict(id=row['id'], fields=sorted(edits[str(row['id'])]['values']),
                 current={key: row[key] for key in ('source_text', 'text', 'start', 'end')},
                 range=dict(start=row['start'], end=row['end']), reason='手工修改待核对')
            for row in rows if edits.get(str(row['id']), {}).get('values')]


def view(uid, version_id=None):
    from dubforge.pipeline import assembly_versions as av
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.pipeline.subtitle_document import read
    from dubforge.pipeline.output_cuts import merge_ranges
    from dubforge import comment_studio
    from dubforge.pipeline.assembly_change_groups import group, saved_comments, compare_comments, global_scope
    project = crud.get_project(uid)
    if not project:
        raise av.VersionError('项目不存在', code='PROJECT_NOT_FOUND')
    deps = Dependencies(project)
    catalog, records = av.catalog(uid)['versions'], av.results(uid)
    artifacts = {art.id: art for art in deps.arts.values()}
    baselines = []
    for definition in catalog:
        record = records.get(definition['id'])
        if not record:
            continue
        artifact = artifacts.get(record.artifact_id)
        baselines.append(dict(version_id=definition['id'], name=definition['name'],
            artifact_id=record.artifact_id, artifact_hash=record.artifact_hash,
            created_at=artifact.created_at.isoformat() if artifact else ''))
    # 同一产物行会在重制时更新，不能按目录顺序或 artifact_id 推断“上一次”。
    baselines.sort(key=lambda row: (row['created_at'], row['artifact_id']), reverse=True)
    if not baselines:
        return dict(baselines=[], baseline=None, coverage='unknown', sentences=[], global_changes=[],
                    manual_candidates=[], comments=[], needs_update=False, whole_render_changed=False,
                    full_rebuild_reasons=[], partial_unavailable_reasons=['尚无成功成片可比较。'],
                    groups=[group(kind, label, [], known=False) for kind, label in
                            [('subtitle', '字幕'), ('audio', '声音'), ('comment', '上屏评论'), ('cuts', '成片剪辑'), ('settings', '呈现与输出设置')]],
                    summary=dict(changed=0, unknown=0, dubbing_pending=0, total_changes=0),
                    suggestion=dict(line_ids=[], ranges=[], replace_audio=False, can_partial=False, reason='尚无成功成片可比较。'))
    version_id = version_id or baselines[0]['version_id']
    if version_id not in {row['version_id'] for row in baselines}:
        raise av.VersionError('请选择本项目已有成功成片的版本。', code='ASSEMBLY_SENTENCE_BASELINE')
    definition = next(v for v in catalog if v['id'] == version_id)
    record = records[version_id]
    saved = json.loads(record.snapshot)
    baseline = next(row for row in baselines if row['version_id'] == version_id)
    artifact = av.result_artifact(deps, definition, records)
    path = deps.path(artifact.step_key, artifact.name) if artifact else None
    baseline['file_exists'] = bool(path and path.is_file() and path.stat().st_size == artifact.size and artifact.sha256 == record.artifact_hash)
    overlay = comment_studio.effective_overlay(uid, comment_studio._read(project).get('overlay'))
    # 对照当前完整呈现与实际成片；局部子版的区间外仍按继承的真实旧输入比较。
    plain = {k: copy.deepcopy(v) for k, v in definition.items() if k not in {'partial', 'update'}}
    version = av.resolve(deps, plain, comment_plan=overlay)
    current = capture(deps, version)
    previous = saved.get('sentence_inputs')
    known = _valid(previous)
    coverage = previous.get('coverage', 'known') if known else 'unknown'
    raw = read(deps, translation=False)
    pending = _dubbing_pending(deps, version, raw)
    comment_changes, comments_known = compare_comments(saved_comments(saved), current['comment_inputs'])
    global_changes = []
    if known:
        old_globals, new_globals = global_scope(previous['global_inputs']), global_scope(current['global_inputs'])
        for key, label in [('source', '原视频已变化'), ('dimensions', '成片呈现方式已变化'),
            ('values', '全局字幕、混音或输出设置已变化'), ('output_cuts', '成片剪辑时间轴已变化'),
            ('background', '背景音轨已变化'), ('redaction', '内容审查范围已变化')]:
            if old_globals.get(key) != new_globals.get(key):
                global_changes.append(label)
        if previous['global_inputs'].get('audio_redaction') != current['global_inputs'].get('audio_redaction'):
            global_changes.append('审查声音处理方式或背景来源已变化')
        if previous['global_inputs'].get('audio_mix_inputs') != current['global_inputs'].get('audio_mix_inputs'):
            global_changes.append('整片声音处理采用的声音集合或排布已变化，其他句的增益也可能改变')
        if previous.get('mixed_globals'):
            global_changes.append('此局部成片保留了不同的呈现设置，需完整生成才能统一')
        if (version.get('output_cuts') and previous['global_inputs'].get('output_cuts')
                and previous['global_inputs'].get('output_cuts_renderer') != current['global_inputs'].get('output_cuts_renderer')):
            global_changes.append('剪辑渲染已修正，需更新成片以应用尾帧修复')
    sentences = []
    old = previous['lines'] if known else {}
    for key in sorted(set(old) | set(current['lines']), key=int):
        a, b = old.get(key), current['lines'].get(key)
        row_known = known and (a is None or a.get('known', True))
        kinds, reasons = [], []
        if row_known:
            for part, label in [('subtitle', '字幕文字或显示时间已变化'), ('audio', '采用的声音、音量或播放位置已变化')]:
                if _normalize((a or {}).get(part, [])) != _normalize((b or {}).get(part, [])):
                    kinds.append(part if part == 'subtitle' else 'audio')
                    reasons.append(label)
        sentences.append(dict(id=int(key), status='changed' if reasons else 'unchanged' if row_known else 'unknown',
            change_kinds=kinds, change_reasons=reasons, current_range=(b or {}).get('range'), baseline_range=(a or {}).get('range'),
            before=copy.deepcopy((a or {}).get('document')), current=copy.deepcopy((b or {}).get('document')),
            before_events=copy.deepcopy((a or {}).get('subtitle', [])), current_events=copy.deepcopy((b or {}).get('subtitle', [])),
            dubbing_pending=int(key) in pending, dubbing_blocking=pending.get(int(key), {}).get('blocking', False),
            dubbing_change_reasons=pending.get(int(key), {}).get('reasons', [])))
    changed = [row for row in sentences if row['status'] == 'changed']
    if coverage == 'known' and any(row['status'] == 'unknown' for row in sentences):
        coverage = 'partial'
    ranges = merge_ranges([span for row in changed + comment_changes for span in (row['current_range'], row['baseline_range']) if span and span['end'] > span['start']])
    blockers = ([row['id'] for row in changed if row['dubbing_blocking']])
    timeline_supported = not version.get('output_cuts') and not saved.get('review_render')
    line_ids = [r['id'] for r in changed if str(r['id']) in current['lines']]
    within_limits = len(ranges) <= 200 and len(line_ids) <= 10000
    # 保存过的呈现参数与剪辑本身也是严格证据，不用当前字幕猜回旧画面。
    old_values = previous['global_inputs']['values'] if known else av.active_values(saved)
    new_values = current['global_inputs']['values']
    fields = {row['path']: row['label'] for row in av.fields()}
    setting_changes = [dict(id=key, reason=fields.get(key, key), before=old_values.get(key), current=new_values.get(key))
                       for key in sorted(set(old_values) | set(new_values)) if old_values.get(key) != new_values.get(key)]
    for key, label in [('dubbing', '声音类型'), ('burn_subtitles', '烧录字幕'), ('include_comments', '上屏评论开关')]:
        if saved.get(key) != version[key]:
            setting_changes.append(dict(id=key, reason=label, before=saved.get(key), current=version[key]))
    if known:
        for key, ident, label in [('source', 'source_media', '源视频素材'), ('background', 'background_media', '背景音轨'),
                                  ('redaction', 'review_processing', '内容审查处理')]:
            if previous['global_inputs'].get(key) != current['global_inputs'].get(key):
                setting_changes.append(dict(id=ident, reason=label + '已变化', before='成片采用的' + label, current='当前' + label))
        if (previous['global_inputs'].get('audio_redaction') != current['global_inputs'].get('audio_redaction')
                and not any(row['id'] == 'review_processing' for row in setting_changes)):
            setting_changes.append(dict(id='review_processing', reason='审查声音处理已变化', before='成片声音处理', current='当前声音处理'))
    cuts_before = (previous['global_inputs'].get('output_cuts') if known else saved.get('output_cuts'))
    cuts_after = version.get('output_cuts')
    cut_changes = ([dict(id='output_cuts', reason='删除片段或成片时间轴已变化', before=cuts_before, current=cuts_after)]
                   if cuts_before != cuts_after else [])
    if (known and version.get('output_cuts') and previous['global_inputs'].get('output_cuts')
            and previous['global_inputs'].get('output_cuts_renderer') != current['global_inputs'].get('output_cuts_renderer')):
        cut_changes.append(dict(id='output_cuts_renderer', reason='剪辑渲染已修正，需应用尾帧修复',
            before='旧剪辑渲染规则', current='尾帧修正后的剪辑渲染规则'))
    if setting_changes and '全局字幕、混音或输出设置已变化' not in global_changes:
        global_changes.append('全局字幕、混音或输出设置已变化')
    if cuts_before != cuts_after and '成片剪辑时间轴已变化' not in global_changes:
        global_changes.append('成片剪辑时间轴已变化')
    has_changes = bool(changed or comment_changes or global_changes)
    can_partial = bool((changed or comment_changes) and not global_changes and coverage == 'known' and comments_known and not blockers
                       and baseline['file_exists'] and timeline_supported and within_limits)
    groups = []
    for kind, label, part in [('subtitle', '字幕', 'subtitle'), ('audio', '声音', 'audio')]:
        details = []
        for row in changed:
            if part not in row['change_kinds']:
                continue
            a, b = old.get(str(row['id'])) or {}, current['lines'].get(str(row['id'])) or {}
            details.append(dict(id=kind + ':' + str(row['id']), line_id=row['id'],
                reason='字幕文字或显示时间已变化' if kind == 'subtitle' else '采用的声音、音量或播放位置已变化',
                before=row['before'] if kind == 'subtitle' else a.get('audio', []),
                current=row['current'] if kind == 'subtitle' else b.get('audio', []),
                baseline_range=row['baseline_range'], current_range=row['current_range']))
        groups.append(group(kind, label, details, known=coverage == 'known' or kind == 'audio' and not version['dubbing'] and not saved.get('dubbing'),
                            reason='旧成片没有完整逐句输入快照，不能据此判断其他句一致。' if coverage != 'known' else ''))
    groups.extend([group('comment', '上屏评论', comment_changes, known=comments_known),
                   group('cuts', '成片剪辑', cut_changes), group('settings', '呈现与输出设置', setting_changes)])
    if known and any('audio' in row['change_kinds'] for row in changed):
        # 铺轨包含整轨峰值/响度处理。明确作为联动选择，不能声称只改变某句而
        # 隐藏其他句最终增益可能随整轨测量改变的影响。
        settings_group = groups[-1]
        settings_group['changes'].append(dict(id='audio_processing', reason='整条音轨重新混音与响度处理',
            before='保留成片中的完整音轨', current='应用所选声音后重算整轨处理，其他句音量也可能变化'))
        settings_group.update(status='changed', count=len(settings_group['changes']))
    partial_unavailable_reasons = list(global_changes)
    if coverage != 'known' or not comments_known:
        partial_unavailable_reasons.append('旧成片缺少完整输入快照，无法自动定位全部历史改动；可按需完整生成建立对照。')
    if blockers:
        partial_unavailable_reasons.append('相关句子的配音尚未更新，请先更新配音。')
    if not timeline_supported:
        partial_unavailable_reasons.append('该基准包含剪辑或旧审查修订，局部替换暂不支持此时间轴。')
    if not within_limits:
        partial_unavailable_reasons.append('变化超过局部修订的 200 段或 10000 句上限。')
    if not baseline['file_exists']:
        partial_unavailable_reasons.append('基准成片文件缺失。')
    whole_render_changed = has_changes
    if coverage != 'known' or not comments_known:
        try:
            whole_render_changed = not av.is_current(deps, av.resolve(deps, definition, comment_plan=overlay))
        except av.DubForgeError:
            whole_render_changed = True
    needs_update = bool(whole_render_changed or not baseline['file_exists'])
    # “本次有变化”和“此基准支持局部更新”是两条独立的判断。
    # 已剪辑但输入一致时只是没有局部能力，不能因此要求再次生成。
    full_rebuild_reasons = partial_unavailable_reasons.copy() if needs_update and not can_partial else []
    reason = (
        ('现有成片的整体输入指纹一致，但缺少完整历史输入对照；可按需完整生成建立对照。'
         if coverage != 'known' or not comments_known else '当前实际采用的输入已应用到该成片，无需更新。')
        if not needs_update else
        '全局呈现或时间轴变化，请完整生成。' if global_changes else
        '旧成片缺少完整输入快照，无法自动识别全部历史差异；仍可手动选择待核对范围。' if coverage != 'known' or not comments_known else
        '请先应用待更新的配音，再检查成片修订范围。' if blockers else
        '该基准包含剪辑或旧审查修订，请完整生成或另选基准。' if not timeline_supported else
        '变化超过局部修订的 200 段或 10000 句上限，请分批选择或完整生成。' if not within_limits else
        '基准成片文件缺失，请先核对文件。' if not baseline['file_exists'] else
        '可检查局部修订，范围包含旧、新字幕、声音及评论区域。')
    from dubforge.pipeline.assembly_updates import decorate
    return decorate(dict(baselines=baselines, baseline=baseline, coverage=coverage, sentences=sentences, global_changes=global_changes,
        groups=groups, comments=comment_changes, whole_render_changed=whole_render_changed, needs_update=needs_update,
        partial_unavailable_reasons=partial_unavailable_reasons,
        manual_candidates=_manual_candidates(project, raw, coverage), full_rebuild_reasons=full_rebuild_reasons,
        summary=dict(changed=len(changed), unknown=sum(r['status'] == 'unknown' for r in sentences),
                     dubbing_pending=len(pending), total_changes=sum(g['count'] for g in groups)), suggestion=dict(line_ids=line_ids,
                     ranges=ranges, replace_audio=any('audio' in r['change_kinds'] for r in changed),
                     blocked_line_ids=blockers, can_partial=can_partial, reason=reason)))
