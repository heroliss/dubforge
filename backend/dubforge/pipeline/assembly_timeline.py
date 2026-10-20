"""正式成片剪辑：冻结已生成媒体，只应用源时间轴上的删除和恢复。"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import uuid

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.utils.fs import atomic_replace, atomic_write_bytes

_ID = re.compile(r'[0-9a-f]{32}')
_GATE = threading.RLock()
RENDER_VERSION = 2


class TimelineError(DubForgeError):
    code = 'ASSEMBLY_TIMELINE_INVALID'
    suggestion = '请重新检查当前成片和删除/恢复清单；旧成片不会被覆盖。'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _deps(uid):
    from dubforge.pipeline.dependencies import Dependencies
    project = crud.get_project(uid)
    if not project:
        raise TimelineError('项目不存在', code='PROJECT_NOT_FOUND')
    return Dependencies(project)


def _prepare_encoding(data, base_path):
    """仅纯删除使用接缝编码；按实际帧边界保存源坐标，避免多次剪辑漂移。"""
    from dubforge.pipeline import segment_cut
    full = dict(method='full_encode', copied_seconds=0., encoded_seconds=data['output_duration'],
        output_duration=data['output_duration'], audio_mode='encode_once' if data['media'][0]['has_audio'] else 'none',
        fallback_reason='恢复片段或混合来源需要完整统一编码。', fallback_code='SEGMENT_CUT_MIXED_SOURCES')
    if (data['restored_ranges'] or len(data['media']) != 1
            or any(row['media_kind'] != 'target' for row in data['segments'])):
        data['encoding'] = full
        return
    from dubforge.pipeline.output_cuts import merge_ranges
    keep = merge_ranges([dict(start=row['media_start'], end=row['media_end']) for row in data['segments']])
    analysis = segment_cut.analyze(base_path, keep)
    data['requested_ranges'] = copy.deepcopy(data['ranges'])
    if analysis.get('keep_ranges'):
        # 既有成片可能已剪过：媒体坐标内取帧后，要逐段映回原片源坐标。
        updated, cursor = [], 0.
        for row in data['segments']:
            for kept in analysis['keep_ranges']:
                left, right = max(row['media_start'], kept['start']), min(row['media_end'], kept['end'])
                if right <= left + 1e-9:
                    continue
                item = copy.deepcopy(row)
                item.update(media_start=left, media_end=right,
                    source_start=round(row['source_start'] + left - row['media_start'], 9),
                    source_end=round(row['source_start'] + right - row['media_start'], 9),
                    virtual_start=cursor, output_start=cursor,
                    virtual_end=cursor + right - left, output_end=cursor + right - left)
                cursor += right - left
                updated.append(item)
        applied, source_cursor = [], 0.
        for row in updated:
            if row['source_start'] > source_cursor + 1e-9:
                applied.append(dict(start=source_cursor, end=row['source_start']))
            source_cursor = max(source_cursor, row['source_end'])
        if data['source_duration'] > source_cursor + 1e-9:
            applied.append(dict(start=source_cursor, end=data['source_duration']))
        data.update(segments=updated, output_duration=analysis['output_duration'], ranges=applied,
                    removed_duration=data['source_duration'] - analysis['output_duration'])
        data['output_cuts'] = dict(schema=1, source_fingerprint=data['source_fingerprint'],
            source_duration=data['source_duration'], output_duration=data['output_duration'], ranges=copy.deepcopy(applied))
        if analysis['method'] == 'smart_cut':
            data['_smart_cut'] = analysis
    data['encoding'] = segment_cut.summary(analysis)
    data['encoding']['audio_mode'] = 'encode_once' if data['media'][0]['has_audio'] else 'none'
    data['encoding']['quantized_ranges'] = copy.deepcopy(data['ranges'])
    if analysis.get('boundary_quantization_seconds', 0.) > 1e-7:
        boundary = analysis['boundary_quantization_seconds']
        amount = f'{boundary * 1000:.2f} 毫秒' if boundary < .001 else f'{boundary:.3f} 秒'
        data['warnings'].append(f'删除边界已按帧向外对齐，额外删除约 {amount}；'
                                '实际范围已写入本版本剪辑记录。')
    if analysis.get('source_tail_gap_seconds', 0.) > 1e-7:
        data['warnings'].append(f'成片末尾实际没有视频帧的 {analysis["source_tail_gap_seconds"]:.3f} 秒已从映射中扣除，'
                                '避免后续剪辑沿用不存在的尾部时间。')
    data['warnings'][0] = ('仅重编码删除接缝，完整 GOP 画面直接复制；音轨按实际保留范围裁剪后只编码一次 AAC。'
        if analysis['method'] == 'smart_cut' else '画面需要完整重新编码；音轨精确裁剪后只编码一次，不重新混音或应用当前制作设置。')


def plan(uid, body):
    """复用试听的来源校验与源坐标拆分，不读取当前字幕/评论或配音。"""
    allowed = {'base_version_id', 'version_id', 'source_fingerprint', 'draft', 'donor', 'name', 'fingerprint'}
    if (not isinstance(body, dict) or set(body) - allowed
            or not isinstance(body.get('base_version_id') or body.get('version_id'), str)):
        raise TimelineError('请选择已生成成片，并检查删除/恢复清单。')
    from dubforge.pipeline import assembly_versions as av, virtual_preview as preview
    try:
        value = preview._build_plan(uid, body)
    except DubForgeError as exc:
        raise TimelineError(exc.message, code=exc.code, detail=exc.detail) from exc
    base_id = body.get('base_version_id') or body.get('version_id')
    record = av.results(uid).get(base_id)
    if not record:
        raise TimelineError('基准成片没有成功记录，请重新选择。')
    try:
        saved = json.loads(record.snapshot)
        definition = av.validate({key: saved[key] for key in
            ('id', 'name', 'dubbing', 'burn_subtitles', 'include_comments', 'overrides')})
        if not isinstance(saved.get('values'), dict):
            raise ValueError('settings')
    except (ValueError, KeyError, TypeError, DubForgeError) as exc:
        raise TimelineError('基准成片缺少有效制作记录，无法建立剪辑版本。') from exc
    from dubforge.pipeline.video_encoding import require_software_baseline
    require_software_baseline(saved)
    deps = _deps(uid)
    base_artifact = av.result_artifact(deps, {'id': base_id}, {base_id: record})
    base_path = deps.path(base_artifact.step_key, base_artifact.name)
    base_key = str(base_path.resolve())
    base_info = None
    if value['base_ranges'] == value['draft_ranges']:
        raise TimelineError('删除清单与基准成片相同，无需生成新的剪辑版本。', code='ASSEMBLY_TIMELINE_UNCHANGED')
    media = {}
    for segment in value['segments']:
        path = Path(segment['path'])
        key = str(path.resolve())
        if key not in media:
            info = preview._probe(path)
            size, digest = preview._path_hash(path)
            media[key] = dict(path=key, size=size, sha256=digest, **info)
            if key == base_key:
                base_info = info
        segment['path'] = key
    audio = {item['has_audio'] for item in media.values()}
    if len(audio) != 1:
        raise TimelineError('基准成片与恢复来源的音轨不一致，请选择具有相同音轨的来源。',
                            code='ASSEMBLY_TIMELINE_AUDIO_MISMATCH')
    if base_info is None:
        base_info = preview._probe(base_path)
    source_fp = value['source_fingerprint']
    cuts = (dict(schema=1, source_fingerprint=source_fp, source_duration=value['source_duration'],
        output_duration=value['duration'], ranges=copy.deepcopy(value['draft_ranges']))
        if value['draft_ranges'] else None)
    restored = [dict(start=row['source_start'], end=row['source_end'])
                for row in value['segments'] if row['media_kind'] != 'target']
    from dubforge.pipeline.output_cuts import merge_ranges
    restored = merge_ranges(restored)
    data = dict(schema=1, project_uid=uid, base_version_id=base_id, base_name=saved['name'],
        base_hash=record.artifact_hash, base_version=saved, definition=definition,
        source_fingerprint=source_fp, source_duration=value['source_duration'],
        output_duration=value['duration'], removed_duration=round(value['source_duration'] - value['duration'], 6),
        ranges=copy.deepcopy(value['draft_ranges']), base_ranges=copy.deepcopy(value['base_ranges']),
        restored_ranges=restored, segments=value['segments'], media=list(media.values()), output_cuts=cuts,
        canvas=dict(width=base_info['width'], height=base_info['height']),
        renderer=RENDER_VERSION,
        summary='只应用当前删除/恢复清单；保留基准成片已有字幕、评论和声音，另存为新版本。',
        warnings=['精确剪辑会重新编码画面和音轨，沿用基准画质，不重新混音或应用当前制作设置。'])
    _prepare_encoding(data, base_path)
    data['fingerprint'] = _digest(data)
    return data


def public(value):
    return {key: copy.deepcopy(value[key]) for key in ('base_version_id', 'base_name', 'source_duration',
        'output_duration', 'removed_duration', 'ranges', 'base_ranges', 'restored_ranges', 'summary', 'warnings', 'fingerprint', 'encoding')}


def create(uid, body):
    from dubforge.project_archive import assert_active
    with _GATE:
        assert_active(uid)
        value = plan(uid, body)
        if body.get('fingerprint') != value['fingerprint']:
            raise TimelineError('成片或删除/恢复清单已变化，请重新检查。', code='ASSEMBLY_TIMELINE_RECHECK')
        name = body.get('name') or (value['base_name'][:65] + ' · 剪辑版')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise TimelineError('请填写 1–80 字的版本名称。')
        deps = _deps(uid)
        folder = deps.root / 'output' / '.timeline-media'
        folder.mkdir(parents=True, exist_ok=True)
        replacements = {}
        for media in value['media']:
            old = Path(media['path'])
            destination = folder / (media['sha256'] + '.mp4')
            if not destination.is_file():
                pending = folder / ('.tmp-' + uuid.uuid4().hex + '.mp4')
                try:
                    try:
                        os.link(old, pending)
                    except OSError:
                        shutil.copyfile(old, pending)
                    if pending.stat().st_size != media['size'] or _hash(pending) != media['sha256']:
                        raise TimelineError('保存剪辑来源期间文件变化，请重新检查。', code='ASSEMBLY_TIMELINE_MEDIA_CHANGED')
                    atomic_replace(pending, destination)
                finally:
                    pending.unlink(missing_ok=True)
            elif destination.stat().st_size != media['size'] or _hash(destination) != media['sha256']:
                raise TimelineError('保存的剪辑来源副本已变化，请重新检查。', code='ASSEMBLY_TIMELINE_MEDIA_CHANGED')
            replacements[str(old)] = str(destination)
            media['path'] = str(destination)
        for segment in value['segments']:
            segment['path'] = replacements[segment['path']]
        plan_id = uuid.uuid4().hex
        value['id'] = plan_id
        value['fingerprint'] = _digest({key: item for key, item in value.items() if key != 'fingerprint'})
        assert_active(uid)
        atomic_write_bytes(deps.root / 'output' / '.timeline-plans' / (plan_id + '.json'),
                           json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
        version = {**value['definition'], 'id': 'timeline-' + plan_id, 'name': name.strip(), 'timeline': {'plan_id': plan_id}}
        return dict(**public(value), version=version)


def load_plan(deps, version, *, verify=False):
    ref = version.get('timeline')
    if (not isinstance(ref, dict) or set(ref) != {'plan_id'} or not isinstance(ref['plan_id'], str)
            or not _ID.fullmatch(ref['plan_id'])):
        raise TimelineError('正式剪辑方案引用无效。')
    try:
        path = deps.root / 'output' / '.timeline-plans' / (ref['plan_id'] + '.json')
        value = json.loads(path.read_text(encoding='utf-8'))
        if (value['schema'] != 1 or value['id'] != ref['plan_id'] or value['project_uid'] != deps.project.uid
                or value['fingerprint'] != _digest({key: item for key, item in value.items() if key != 'fingerprint'})):
            raise ValueError('identity')
        if any(version.get(key) != value['definition'].get(key) for key in
               ('dubbing', 'burn_subtitles', 'include_comments', 'overrides')):
            raise TimelineError('剪辑版本保留来源的声音、字幕、评论和画质；修改制作参数请创建普通成片版本。',
                                code='ASSEMBLY_TIMELINE_FROZEN')
        expected_paths = set()
        for media in value['media']:
            if not re.fullmatch('[0-9a-f]{64}', media['sha256']):
                raise ValueError('hash')
            expected = deps.root / 'output' / '.timeline-media' / (media['sha256'] + '.mp4')
            if (Path(media['path']).resolve() != expected.resolve() or not expected.is_file()
                    or expected.stat().st_size != media['size']):
                raise ValueError('media')
            if verify and _hash(expected) != media['sha256']:
                raise ValueError('changed')
            expected_paths.add(str(expected))
        if not value['segments'] or any(row['path'] not in expected_paths for row in value['segments']):
            raise ValueError('segments')
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TimelineError('本项目找不到有效的正式剪辑方案或来源文件已变化，请重新建立剪辑版本。',
                            code='ASSEMBLY_TIMELINE_PLAN_MISSING') from exc


def resolve(deps, version):
    value = load_plan(deps, version)
    saved = value['base_version']
    from dubforge.pipeline.video_encoding import require_software_baseline
    require_software_baseline(saved)
    # 声音、字幕、评论均已烙入来源媒体；此处值只描述基准，不读取工作稿。
    return {**version, 'values': copy.deepcopy(saved['values']), 'origins': copy.deepcopy(saved.get('origins', {})),
        'comment_overlay': None, 'output_cuts': copy.deepcopy(value['output_cuts']), 'output_cuts_scope': 'timeline',
        'output_cuts_error': '', 'audio_line_gains': {}, 'timeline_plan': value}


def _render_full(ctx, out, value):
    """兼容路径：完整精确拼接，不二次添加字幕或声音处理。"""
    from dubforge.pipeline.render_resources import bounded_ffmpeg_command, ffmpeg_failure
    from dubforge.utils.ffmpeg import complex_filter_command
    from dubforge.execution_progress import report_stage
    from dubforge.pipeline.output_cuts import mux_timing_options
    media = value['media']
    index = {row['path']: i for i, row in enumerate(media)}
    first = media[0]
    width, height, audio = value['canvas']['width'], value['canvas']['height'], first['has_audio']
    command = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-progress', 'pipe:1', '-nostats']
    for row in media:
        command += ['-i', row['path']]
    graph, labels = [], []
    for i, segment in enumerate(value['segments']):
        inp, start, end = index[segment['path']], segment['media_start'], segment['media_end']
        graph.append(f'[{inp}:v:0]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,'
                     f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
                     f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]')
        if audio:
            graph.append(f'[{inp}:a:0]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,'
                         f'aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]')
        labels.append(f'[v{i}]' + (f'[a{i}]' if audio else ''))
    graph.append(''.join(labels) + f'concat=n={len(labels)}:v=1:a={int(audio)}[vout]' + ('[aout]' if audio else ''))
    command += ['-filter_complex', ';'.join(graph), '-map', '[vout]', '-c:v', 'libx264', '-preset', 'medium',
                '-crf', str(ctx.cfg('assemble.crf', 18)), '-pix_fmt', 'yuv420p']
    if audio:
        command += ['-map', '[aout]', '-c:a', 'aac', '-b:a', '192k']
    command += ['-map_chapters', '-1', *mux_timing_options(value['output_duration']), '-movflags', '+faststart', str(out)]
    report_stage(ctx, 'encoding_timeline', '按删除/恢复清单生成剪辑成片，保留来源已有内容',
                 completed=0, total=value['output_duration'], unit='成片秒')
    from dubforge.progress import RateProgress
    meter = RateProgress(ctx, value['output_duration'], start=0., end=98., scope='phase')
    def progress(line):
        if line.startswith('out_time_us='):
            try:
                seconds = min(value['output_duration'], max(0., float(line.partition('=')[2]) / 1_000_000))
            except ValueError:
                return
            report_stage(ctx, 'encoding_timeline', '按删除/恢复清单生成剪辑成片',
                         completed=round(seconds, 3), total=value['output_duration'], unit='成片秒')
            meter.update(seconds)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    ctx.check_cancel(); ctx.check_pause()
    with complex_filter_command(bounded_ffmpeg_command(command), Path(out).parent) as prepared:
        code, _, error = ctx.run_subprocess(prepared, cwd=Path(out).parent, on_stdout=progress)
    if code:
        raise ffmpeg_failure(error, output_dir=Path(out).parent)
    ctx.check_cancel()


def _finish(ctx, out, value, audio):
    clear_eta = getattr(ctx, "clear_phase_eta", None)
    if callable(clear_eta):
        clear_eta()
    from dubforge.execution_progress import report_stage
    report_stage(ctx, 'checking_timeline_output', '核对剪辑成片的时长、画幅与音轨')
    width, height = value['canvas']['width'], value['canvas']['height']
    from dubforge.pipeline.virtual_preview import _probe
    final = _probe(Path(out))
    if (abs(final['duration'] - value['output_duration']) > .08 or final['has_audio'] != audio
            or final['width'] != width or final['height'] != height):
        raise TimelineError('剪辑产物的时长、画幅或音轨与方案不一致，已保留旧成片，请重新检查来源。',
                            code='ASSEMBLY_TIMELINE_OUTPUT_MISMATCH')
    from dubforge.pipeline.audio_quality import measure, settings
    ctx._audio_report = (dict(**measure(out, ctx=ctx), has_audio=True, preview=False, settings=settings(ctx.cfg),
        processing={'gain_db': 0., 'timeline_edit': True}, target_reached=None, line_report=[])
        if audio else dict(schema=1, has_audio=False, preview=False, issues=[]))
    ctx._audio_report['measurement_provenance'] = dict(kind='timeline_sources',
        reason='只拼接来源已有声音，未重新混音、均衡或应用响度目标。')
    if audio:
        ctx._audio_report.setdefault('issues', []).append(dict(kind='timeline_sources', severity='info',
            message='音轨沿用剪辑来源并同步裁剪；未重新应用当前混音或响度设置。'))


def render(ctx, out):
    """优先复用主体 GOP；不能证明安全时明确说明并改用完整编码。"""
    from dubforge.pipeline import segment_cut, segment_replace
    from dubforge.pipeline.render_resources import preflight_render
    from dubforge.execution_progress import report_stage
    value = load_plan(ctx.dependencies(), ctx.version, verify=True)
    preflight_render(ctx, Path(out).parent)
    actual = copy.deepcopy(value.get('encoding') or dict(method='full_encode', copied_seconds=0.,
        encoded_seconds=value['output_duration'], output_duration=value['output_duration'],
        audio_mode='encode_once' if value['media'][0]['has_audio'] else 'none'))
    smart = value.get('_smart_cut')
    completed = False
    if smart and actual['method'] == 'smart_cut':
        try:
            actual = segment_cut.render(ctx, Path(value['media'][0]['path']), out, smart,
                                         crf=int(ctx.cfg('assemble.crf', 18)))
            completed = True
        except segment_replace.SegmentReplaceError as exc:
            compatible_failures = {'SEGMENT_REPLACE_UNSUPPORTED', 'SEGMENT_REPLACE_INCOMPATIBLE',
                'SEGMENT_REPLACE_FRAME_COUNT', 'SEGMENT_REPLACE_TIMESTAMPS', 'SEGMENT_CUT_DURATION'}
            if exc.code not in compatible_failures:
                raise
            reason = '接缝编码校验未通过，改用完整编码：' + exc.message
            actual.update(method='full_encode', copied_seconds=0., encoded_seconds=value['output_duration'],
                          fallback_reason=reason, fallback_code=exc.code)
            report_stage(ctx, 'smart_cut_fallback', '接缝编码无法安全复用，切换完整编码', detail=reason)
            ctx.log(reason)
            ctx.check_cancel()
    if not completed:
        _render_full(ctx, out, value)
    _finish(ctx, out, value, value['media'][0]['has_audio'])
    actual['quantized_ranges'] = copy.deepcopy(value['ranges'])
    ctx.version['actual_encoding'] = actual


def completed_snapshot(version):
    """保留基准真实呈现记录，不能用当前工作稿冒充剪辑成片内容。"""
    value = version['timeline_plan']
    saved = value['base_version']
    result = copy.deepcopy(saved.get('sentence_inputs'))
    if isinstance(result, dict):
        globals_ = result.get('global_inputs', {})
        if value['output_cuts']:
            from dubforge.pipeline.output_cuts import RENDER_VERSION as CUT_RENDER_VERSION
            globals_['output_cuts'] = copy.deepcopy(value['output_cuts'])
            globals_['output_cuts_renderer'] = CUT_RENDER_VERSION
        else:
            globals_.pop('output_cuts', None); globals_.pop('output_cuts_renderer', None)
        # 恢复区间来自不同的已烙入媒体，不能声称沿用基准逐句文字和声音。
        for row in result.get('lines', {}).values():
            span = row.get('range') or row.get('source_range')
            if span and any(span['start'] < x['end'] and span['end'] > x['start'] for x in value['restored_ranges']):
                row['known'] = False
        if value['restored_ranges']:
            result['coverage'] = 'partial'
            result['mixed_globals'] = True
            comments = result.get('comment_inputs')
            if isinstance(comments, dict):
                comments['known'] = False
    return result
