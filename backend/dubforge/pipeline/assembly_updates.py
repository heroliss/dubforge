"""内容级成片更新合同：先合并目标输入，再决定编码范围。"""
import copy
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.utils.fs import atomic_replace, atomic_write_bytes

_GATE = threading.RLock()
_REF = re.compile(r'[0-9a-f]{32}')


class UpdateError(DubForgeError):
    code = 'ASSEMBLY_UPDATE_INVALID'
    suggestion = '请重新检查成片差异，再选择本次要应用的改动。'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _plain(spec):
    return {key: copy.deepcopy(value) for key, value in spec.items() if key not in {'partial', 'update', 'timeline'}}


def _events_span(events):
    return [(event['start'], event['end']) for event in events]


def decorate(data):
    """已知差异可选；时间调整与它直接带动的字幕/声音显式联动。"""
    entries = {row['id']: row for group in data.get('groups', []) for row in group['changes']}
    for group in data.get('groups', []):
        for row in group['changes']:
            row['selectable'] = data.get('coverage') == 'known' or group['kind'] in {'settings', 'cuts'}
            row['linked_ids'] = []
            if not row['selectable']:
                row['blocked_reason'] = '缺少可靠的历史内容，暂不能保证单独应用。'
    for sentence in data.get('sentences', []):
        sid, aid = f'subtitle:{sentence["id"]}', f'audio:{sentence["id"]}'
        if sid in entries and aid in entries:
            before, after = sentence.get('before_events', []), sentence.get('current_events', [])
            if _events_span(before) != _events_span(after):
                entries[sid]['linked_ids'].append(aid)
                entries[aid]['linked_ids'].append(sid)
    # 轨道/呈现开关会改变对应的全部实际事件，不能只改变开关后偷偷重算。
    related = {
        'assemble.subtitle_track': ('subtitle:',), 'subtitles.source_first': ('subtitle:',),
        'burn_subtitles': ('subtitle:',), 'include_comments': ('comment:',),
        'dubbing': ('subtitle:', 'audio:'), 'source_media': ('',),
        'review_processing': ('subtitle:', 'audio:', 'comment:'),
        'audio_processing': ('audio:',),
    }
    for ident, prefixes in related.items():
        if ident in entries:
            for other in entries:
                if other != ident and any(other.startswith(prefix) for prefix in prefixes):
                    entries[ident]['linked_ids'].append(other)
                    entries[other]['linked_ids'].append(ident)
    pairs = [('output_cuts', 'output_cuts_renderer')]
    pairs.extend((key, 'review_processing') for key in entries if key.startswith('content_review.'))
    for left, right in pairs:
        if left in entries and right in entries:
            entries[left]['linked_ids'].append(right)
            entries[right]['linked_ids'].append(left)
    for ident, row in entries.items():
        row['linked_ids'] = sorted(set(row['linked_ids']))
    return data


def _copy_line(target, current, line_id, part):
    key = str(line_id)
    old = target['inputs']['lines'].get(key)
    new = current['inputs']['lines'].get(key)
    if old is None:
        old = dict(id=line_id, subtitle=[], audio=[], known=True)
        target['inputs']['lines'][key] = old
    old[part] = copy.deepcopy((new or {}).get(part, []))
    if part == 'subtitle':
        for field in ('document', 'source_range'):
            if field in (new or {}):
                old[field] = copy.deepcopy(new[field])
    name = 'subtitles' if part == 'subtitle' else 'audio'
    target[name] = [event for event in target[name] if event['id'] != line_id]
    target[name].extend(copy.deepcopy([event for event in current[name] if event['id'] == line_id]))
    active = old.get('subtitle', []) + old.get('audio', [])
    if active:
        old['range'] = dict(start=min(e['start'] for e in active), end=max(e['end'] for e in active))


def target_manifest(before, current, entries, selected):
    """仅应用所选字段；保留其他实际事件与素材引用。"""
    target = copy.deepcopy(before)
    globals_ = target['inputs']['global_inputs']
    new_globals = current['inputs']['global_inputs']
    for ident in sorted(selected):
        row, kind = entries[ident]
        if kind in {'subtitle', 'audio'}:
            _copy_line(target, current, row['line_id'], 'subtitle' if kind == 'subtitle' else 'audio')
        elif kind == 'comment':
            comment_id = row['comment_id']
            for container, field in ((target, 'comments'), (target['inputs']['comment_inputs'], 'events')):
                source = current['comments'] if field == 'comments' else current['inputs']['comment_inputs']['events']
                container[field] = [event for event in container[field] if event['id'] != comment_id]
                container[field].extend(copy.deepcopy([event for event in source if event['id'] == comment_id]))
        elif kind == 'cuts':
            target['version']['output_cuts'] = copy.deepcopy(current['version'].get('output_cuts'))
            for key in ('output_cuts', 'output_cuts_renderer'):
                if key in new_globals:
                    globals_[key] = copy.deepcopy(new_globals[key])
                else:
                    globals_.pop(key, None)
        elif ident in {'dubbing', 'burn_subtitles', 'include_comments'}:
            target['version'][ident] = current['version'][ident]
            globals_['dimensions'][ident] = current['version'][ident]
        elif ident == 'source_media':
            target['media']['source'] = copy.deepcopy(current['media']['source'])
            globals_['source'] = copy.deepcopy(new_globals['source'])
            target['duration'] = current['duration']
            target['inputs']['duration'] = current['duration']
        elif ident == 'background_media':
            target['media']['background'] = copy.deepcopy(current['media'].get('background'))
            globals_['background'] = copy.deepcopy(new_globals.get('background'))
        elif ident == 'review_processing':
            target['review'] = copy.deepcopy(current['review'])
            for key in ('redaction', 'audio_redaction'):
                globals_[key] = copy.deepcopy(new_globals.get(key))
        elif ident in current['version']['values'] or ident in target['version']['values']:
            if ident in current['version']['values']:
                target['version']['values'][ident] = copy.deepcopy(current['version']['values'][ident])
            else:
                target['version']['values'].pop(ident, None)
    from dubforge.pipeline.assembly_versions import active_values
    globals_['values'] = active_values(target['version'])
    values = target['version']['values']
    if target['version']['dubbing'] and (values.get('assemble.sentence_leveling') or values.get('assemble.loudnorm')
            or values.get('assemble.final_loudness_mode', 'off') != 'off'):
        globals_['audio_mix_inputs'] = {key: row['audio'] for key, row in target['inputs']['lines'].items()}
    else:
        globals_.pop('audio_mix_inputs', None)
    # 评论采用实际独立事件；旧的等间隔阅读方案不参与成片比较。
    globals_.pop('comments', None)
    for name in ('subtitles', 'audio', 'comments'):
        target[name].sort(key=lambda event: (event['start'], event['end'], str(event['id'])))
    target['inputs']['comment_inputs']['events'].sort(key=lambda event: (event['start'], event['end'], str(event['id'])))
    return target


def _method(before, target, selected, entries):
    from dubforge.pipeline.assembly_change_groups import global_scope
    if (before['version'].get('output_cuts') or target['version'].get('output_cuts')
            or global_scope(before['inputs']['global_inputs']) != global_scope(target['inputs']['global_inputs'])
            or any(entries[ident][1] in {'audio', 'settings', 'cuts'} for ident in selected)):
        return 'full'
    return 'partial'


def _audio_signature(manifest):
    """完整重绘画面不代表允许重混未选音轨；声音合同单独判断。"""
    globals_ = manifest['inputs']['global_inputs']
    values = manifest['version']['values']
    from dubforge.pipeline.video_encoding import ENCODING_KEYS
    audio_values = {key: value for key, value in values.items() if key.startswith('assemble.')
                    and not key.startswith('assemble.subtitle_') and key not in ENCODING_KEYS}
    return dict(dubbing=manifest['version']['dubbing'],
        lines={key: row.get('audio', []) for key, row in manifest['inputs']['lines'].items()},
        values=audio_values, source=globals_.get('source'), background=globals_.get('background'),
        review=manifest['review']['audio_plan'], output_cuts=manifest['version'].get('output_cuts'),
        output_cuts_renderer=globals_.get('output_cuts_renderer'))


def _base(deps, version_id):
    from dubforge.pipeline import assembly_versions as av
    records = av.results(deps.project.uid)
    record = records.get(version_id)
    art = av.result_artifact(deps, {'id': version_id}, records)
    path = deps.path(art.step_key, art.name) if art else None
    if not record or not art or not path or not path.is_file() or art.sha256 != record.artifact_hash:
        raise UpdateError('基准成片文件缺失或登记不一致，请重新选择。', code='ASSEMBLY_UPDATE_BASE_CHANGED')
    from dubforge.pipeline.video_encoding import require_software_baseline
    require_software_baseline(json.loads(record.snapshot))
    stat = path.stat()
    from dubforge.pipeline.assembly_partial import _checked_hash
    if stat.st_size != art.size or _checked_hash(str(path), stat.st_size, stat.st_mtime_ns) != record.artifact_hash:
        raise UpdateError('基准成片已变化，请重新检查差异。', code='ASSEMBLY_UPDATE_BASE_CHANGED')
    return record, art, path


def plan(uid, body):
    if (not isinstance(body, dict) or set(body) - {'base_version_id', 'change_ids', 'fingerprint', 'name'}
            or not isinstance(body.get('base_version_id'), str)
            or not isinstance(body.get('change_ids'), list) or not body['change_ids']
            or len(body['change_ids']) > 30000
            or any(not isinstance(ident, str) or not 1 <= len(ident) <= 250 for ident in body['change_ids'])
            or len(set(body['change_ids'])) != len(body['change_ids'])):
        raise UpdateError('请选择本次需要应用的成片改动。')
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.pipeline import assembly_versions as av, assembly_sentence_changes as changes, assembly_manifest as manifests
    from dubforge import comment_studio
    project = crud.get_project(uid)
    if not project:
        raise UpdateError('项目不存在。', code='PROJECT_NOT_FOUND')
    deps = Dependencies(project)
    comparison = changes.view(uid, body['base_version_id'])
    entries = {row['id']: (row, group['kind']) for group in comparison['groups'] for row in group['changes']}
    selected = set(body['change_ids'])
    if selected - entries.keys():
        raise UpdateError('部分改动已变化或已应用，请重新检查。', code='ASSEMBLY_UPDATE_RECHECK')
    for ident in selected:
        row = entries[ident][0]
        if row.get('selectable') is False:
            raise UpdateError(row.get('blocked_reason') or '此改动暂不可单独应用。', code='ASSEMBLY_HISTORY_UNAVAILABLE')
        missing = set(row.get('linked_ids', [])) - selected
        if missing:
            raise UpdateError('此项与其他改动共用时间或呈现方式，请一同选择关联改动。',
                              code='ASSEMBLY_UPDATE_LINKED', detail=dict(required_ids=sorted(missing)))
    record, art, path = _base(deps, body['base_version_id'])
    saved = json.loads(record.snapshot)
    before = manifests.from_saved(deps, saved)
    definition = next(spec for spec in av.catalog(uid)['versions'] if spec['id'] == body['base_version_id'])
    overlay = comment_studio.effective_overlay(uid, comment_studio._read(project).get('overlay'))
    current = manifests.capture(deps, av.resolve(deps, _plain(definition), comment_plan=overlay))
    target = target_manifest(before, current, entries, selected)
    if target['version']['burn_subtitles']:
        from dubforge.pipeline.subtitle_style import validate_style
        try:
            validate_style(target['version']['values'])
        except DubForgeError as exc:
            raise UpdateError('所选设置与保留的成片设置不兼容：' + exc.message
                              + '。请同时选择相关设置后重新检查。', code='ASSEMBLY_UPDATE_SETTINGS_CONFLICT') from exc
    method = _method(before, target, selected, entries)
    replace_audio = _audio_signature(before) != _audio_signature(target)
    manifests.validate_media(deps, target, audio=replace_audio)
    from dubforge.pipeline.output_cuts import merge_ranges
    ranges = merge_ranges([span for ident in selected for span in
        (entries[ident][0].get('baseline_range'), entries[ident][0].get('current_range'))
        if span and span['end'] > span['start']])
    expanded = ranges
    seconds = target['duration']
    if method == 'partial':
        from dubforge.pipeline.assembly_partial import _inspect
        from dubforge.pipeline.segment_replace import plan_windows
        stat = path.stat()
        info = copy.deepcopy(_inspect(str(path), stat.st_size, stat.st_mtime_ns))
        windows = plan_windows(info, ranges)
        expanded = windows.get('expanded_ranges', windows.get('windows', ranges))
        seconds = windows.get('encode_seconds', sum(r['end'] - r['start'] for r in ranges))
    remaining = len(entries) - len(selected)
    from dubforge.pipeline.audio_quality import read_report
    data = dict(schema=1, project_uid=uid, base_version_id=body['base_version_id'], base_name=saved['name'],
        base_path=str(path), base_sha256=record.artifact_hash, base_size=art.size,
        change_ids=sorted(selected), selection_count=len(selected), remaining_count=remaining,
        method=method, ranges=ranges, expanded_ranges=expanded, encode_seconds=seconds,
        duration=target['duration'], replace_audio=replace_audio,
        definition=_plain(definition), target=target, source_identity=copy.deepcopy(current['media']['source']),
        current_inputs_hash=digest(current['inputs']), base_version=saved,
        baseline_audio_report=read_report(path),
        summary=(f'应用所选 {len(selected)} 项改动；另有 {remaining} 项保留成片内容，继续待更新。'
                 if remaining else f'应用所选 {len(selected)} 项改动，已包含本次识别的全部差异。'),
        warnings=[('所选声音或时间轴设置需要完整生成；仍只应用本次勾选内容。' if replace_audio
                   else '所选呈现设置需要完整重制画面，完整保留成片音轨。')] if method == 'full' else [])
    if 'audio_processing' in selected:
        data['warnings'].append('本次已联动选择整条音轨处理；将保留未改句子的素材与位置，但整轨均衡和响度可能改变其音量。')
    data['fingerprint'] = digest(data)
    return data


def public(value):
    return {key: copy.deepcopy(value[key]) for key in ('fingerprint', 'selection_count', 'remaining_count',
        'method', 'ranges', 'expanded_ranges', 'encode_seconds', 'duration', 'replace_audio', 'base_name', 'summary', 'warnings')}


def create(uid, body):
    from dubforge.project_archive import assert_active
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.pipeline import assembly_manifest as manifests
    with _GATE:
        assert_active(uid)
        value = plan(uid, body)
        if body.get('fingerprint') != value['fingerprint']:
            raise UpdateError('成片或工作稿在检查后发生变化，请重新检查。', code='ASSEMBLY_UPDATE_RECHECK')
        deps = Dependencies(crud.get_project(uid))
        name = body.get('name') or value['base_name']
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise UpdateError('请填写 1–80 字的成片名称。')
        value['target'] = manifests.materialize(deps, value['target'], audio=value['replace_audio'])
        folder = deps.root / 'output' / '.update-baselines'
        destination = folder / (value['base_sha256'] + '.mp4')
        folder.mkdir(parents=True, exist_ok=True)
        from dubforge.pipeline.assembly_partial import _hash
        if not destination.exists():
            pending = folder / ('.tmp-' + uuid.uuid4().hex + '.mp4')
            try:
                try:
                    os.link(value['base_path'], pending)
                except OSError:
                    shutil.copyfile(value['base_path'], pending)
                if _hash(pending) != value['base_sha256']:
                    raise UpdateError('保存基准期间文件变化，请重新检查。', code='ASSEMBLY_UPDATE_BASE_CHANGED')
                atomic_replace(pending, destination)
            finally:
                pending.unlink(missing_ok=True)
        elif destination.stat().st_size != value['base_size'] or _hash(destination) != value['base_sha256']:
            raise UpdateError('保存的基准副本已变化，请重新检查。', code='ASSEMBLY_UPDATE_BASE_CHANGED')
        plan_id = uuid.uuid4().hex
        value.update(id=plan_id, base_path=str(destination))
        value['fingerprint'] = digest({key: item for key, item in value.items() if key != 'fingerprint'})
        assert_active(uid)
        atomic_write_bytes(deps.root / 'output' / '.update-plans' / (plan_id + '.json'),
                           json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
        version = dict(**value['definition'])
        version.update(id=value['base_version_id'], name=name.strip(), update=dict(plan_id=plan_id))
        return dict(**public(value), version=version)


def load_plan(deps, version, *, verify=False):
    ref = version.get('update')
    if not isinstance(ref, dict) or set(ref) != {'plan_id'} or not isinstance(ref['plan_id'], str) or not _REF.fullmatch(ref['plan_id']):
        raise UpdateError('成片更新方案引用无效。')
    path = deps.root / 'output' / '.update-plans' / (ref['plan_id'] + '.json')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        expected = deps.root / 'output' / '.update-baselines' / (value['base_sha256'] + '.mp4')
        if (value['schema'] != 1 or value['id'] != ref['plan_id'] or value['project_uid'] != deps.project.uid
                or not re.fullmatch('[0-9a-f]{64}', value['base_sha256'])
                or value['fingerprint'] != digest({key: item for key, item in value.items() if key != 'fingerprint'})
                or Path(value['base_path']).resolve() != expected.resolve()
                or not expected.is_file() or expected.stat().st_size != value['base_size']):
            raise ValueError('identity')
        if verify:
            from dubforge.pipeline.assembly_partial import _hash
            from dubforge.pipeline import assembly_manifest as manifests
            if _hash(expected) != value['base_sha256']:
                raise UpdateError('更新方案的基准副本已变化，请重新检查。', code='ASSEMBLY_UPDATE_BASE_CHANGED')
            manifests.validate_media(deps, value['target'], audio=value['replace_audio'])
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UpdateError('本项目找不到有效的成片更新方案，请重新检查并建立。', code='ASSEMBLY_UPDATE_PLAN_MISSING') from exc


def resolve(deps, version):
    value = load_plan(deps, version)
    from dubforge.pipeline.video_encoding import require_software_baseline
    require_software_baseline(value['base_version'])
    target = value['target']
    from dubforge.pipeline.assembly_manifest import comment_plan
    return {**version, **copy.deepcopy(target['version']), 'id': version['id'], 'name': version['name'],
            'render_manifest': copy.deepcopy(target), 'update_plan': value,
            'comment_overlay': comment_plan(target)}


def render(ctx, source, out, total):
    """局部编码读取同一目标清单；完整生成由普通版本渲染器消费清单。"""
    value = load_plan(ctx.dependencies(), ctx.version, verify=True)
    from dubforge.pipeline.partial_render import render_segment
    from dubforge.pipeline.segment_replace import replace
    from dubforge.execution_progress import report_stage
    report_stage(ctx, 'applying_selected_changes', '应用勾选内容，保留其他成片内容',
                 total=value['selection_count'], unit='项')
    def produce(start_frame, end_frame, info, target):
        render_segment(ctx, Path(value['base_path']), source, value['ranges'], start_frame, end_frame, info, target)
    replace(ctx, Path(value['base_path']), Path(out), value['ranges'], produce)
    from dubforge.pipeline.audio_quality import read_report
    report = value.get('baseline_audio_report') or read_report(Path(value['base_path']))
    if report is not None:
        ctx._audio_report = copy.deepcopy(report)
        ctx._audio_report['measurement_provenance'] = dict(kind='baseline_audio_reused',
            base_sha256=value['base_sha256'], reason='本次只更新画面，完整保留基片音轨。')


def render_visual_full(ctx, source, out, total):
    """只重绘完整画面，直接复制基片整轨；无需旧逐句WAV或重新测量混音。"""
    import tempfile
    from dubforge.pipeline.step_assemble import _mux_original_sub, _has_audio_stream
    from dubforge.pipeline.render_resources import bounded_ffmpeg_command, ffmpeg_failure
    from dubforge.pipeline.audio_quality import read_report, measure
    from dubforge.execution_progress import report_stage
    value = load_plan(ctx.dependencies(), ctx.version, verify=True)
    with tempfile.TemporaryDirectory(prefix='.tmp_update_visual_', dir=Path(out).parent) as folder:
        video = Path(folder) / 'visual.mp4'
        _mux_original_sub(ctx, source, video, total, (0., 97.), burn=ctx.version['burn_subtitles'],
                          comment_plan=ctx.version['comment_overlay'], video_only=True)
        report_stage(ctx, 'reusing_complete_audio', '画面重制完成，直接保留成片完整音轨', reused=True)
        cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-i', str(video),
               '-i', value['base_path'], '-map', '0:v:0', '-map', '1:a:0?', '-map_chapters', '-1',
               '-c', 'copy', '-movflags', '+faststart', str(out)]
        code, _, error = ctx.run_subprocess(bounded_ffmpeg_command(cmd))
        if code:
            raise ffmpeg_failure(error, output_dir=Path(out).parent)
    report = value.get('baseline_audio_report') or read_report(Path(value['base_path']))
    if report is None:
        report = (dict(**measure(out, ctx=ctx), has_audio=True, preview=False)
                  if _has_audio_stream(Path(out)) else dict(schema=1, has_audio=False, preview=False, issues=[]))
    ctx._audio_report = copy.deepcopy(report)
    ctx._audio_report['measurement_provenance'] = dict(kind='baseline_audio_reused',
        base_sha256=value['base_sha256'], reason='本次完整重绘画面，压缩音轨直接复制，声音未重新处理。')
