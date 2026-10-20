"""成片版本的单一入口：身份、显式继承、冻结、依赖、指纹与产物归属。"""
import copy
import hashlib
import json
import math
import re
from dataclasses import asdict
from datetime import datetime, timezone

from sqlmodel import select

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.assembly_models import AssemblyResult
from dubforge.pipeline.assembly_outputs import dimensions, final_variant, VARIANT_LABELS
from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA, ParamField, source_style_fields
from dubforge.pipeline.video_encoding import ENCODING_KEYS, ENCODER_KEY, CQ_KEY


class VersionError(DubForgeError):
    code = 'ASSEMBLY_VERSION_INVALID'
    suggestion = '请检查成片版本设置后重试；不会修改其他版本或已发布稿件。'


ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')
RENDER_KEYS = {'assemble.voice_volume', 'assemble.bgm_volume', 'assemble.ducking', 'assemble.loudnorm', 'assemble.crf',
               ENCODER_KEY, CQ_KEY,
               'assemble.final_loudness_mode', 'assemble.final_loudness_lufs', 'assemble.final_true_peak_db',
               'assemble.sentence_leveling', 'assemble.sentence_leveling_max_db',
               'subtitles.source_first', 'subtitles.min_duration'} | {
    f.path for f in STEP_PARAM_SCHEMA['assemble'] if f.path.startswith('assemble.subtitle_')}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def fields():
    base = [asdict(f) for group in STEP_PARAM_SCHEMA.values() for f in group if f.path in RENDER_KEYS]
    # 评论内容在评论工作区统一维护，这里只允许覆盖呈现方式。
    comments = [
        ParamField('comments.language_mode', '显示语言', 'select', options=(('中文译文', 'translated'), ('双语', 'bilingual'), ('原文', 'original'))),
        ParamField('comments.source_first', '双语原文在前', 'bool'),
        ParamField('comments.width_percent', '最大宽度（画面 %）', 'float', min=0, max=100, step=1,
                   help='0 自动与 100% 均使用扣除左右边距后的可用宽度，短评论保持自然长度。1–99% 可进一步限制排版宽度；百分比相对于整个画面，仍受边距限制。'),
        ParamField('comments.height_percent', '目标最大高度（画面 %）', 'float', min=1, max=100, step=1,
                   help='先按最大宽度换行，再在字号范围内缩小以接近此高度；达到最小字号后仍放不下则完整扩高，不截断文字。位置锚点和边距保持不变。'),
        ParamField('comments.position', '位置', 'select', options=tuple((label, value) for label, value in
            [('左上', 'top_left'), ('顶部居中', 'top'), ('右上', 'top_right'), ('左下', 'bottom_left'), ('底部居中', 'bottom'), ('右下', 'bottom_right')])),
        ParamField('comments.font', '译文字体（通用）', 'select', editable=True, options=(('微软雅黑', 'Microsoft YaHei'), ('Arial', 'Arial'))),
        ParamField('comments.font_size', '译文最大字号（通用）', 'int', min=18, max=72, step=1,
                   help='按 1080 高画面设定；先使用最大字号，超出目标高度时才向最小字号收缩。原文未开启独立样式时跟随此范围。'),
        ParamField('comments.min_font_size', '译文最小字号（通用）', 'int', min=18, max=72, step=1,
                   help='不能大于最大字号；长评论到达此下限后允许扩高，完整显示文字。按 1080 高画面设定。'),
        ParamField('comments.color', '译文文字颜色', 'color'),
        ParamField('comments.outline_color', '译文描边颜色', 'color'),
        ParamField('comments.outline', '译文描边粗细', 'float', min=0, max=10, step=.5),
        ParamField('comments.background_color', '底色', 'color'),
        ParamField('comments.background_opacity', '底色不透明度', 'float', min=0, max=100, step=1),
        ParamField('comments.margin', '边距', 'int', min=0, max=400, step=1),
        ParamField('comments.show_author', '显示作者', 'bool'),
    ] + list(source_style_fields('comments.', comments=True))
    for item in base + [asdict(f) for f in comments]:
        item['options'] = [dict(label=label, value=value) for label, value in item['options']]
        item['group'] = ('字幕' if item['path'].startswith(('assemble.subtitle_', 'subtitles.')) else
                         '评论' if item['path'].startswith('comments.') else '输出' if item['path'] in ENCODING_KEYS else '声音')
        yield item


def validate(raw):
    if not isinstance(raw, dict) or set(raw) - {'id', 'name', 'dubbing', 'burn_subtitles', 'include_comments', 'overrides', 'partial', 'update', 'timeline'}:
        raise VersionError('成片版本格式无效')
    if not isinstance(raw.get('id'), str) or not ID.fullmatch(raw['id']):
        raise VersionError('成片版本标识无效')
    if not isinstance(raw.get('name'), str) or not 1 <= len(raw['name'].strip()) <= 80:
        raise VersionError('请填写1–80字的版本名称')
    for key in ('dubbing', 'burn_subtitles', 'include_comments'):
        if type(raw.get(key)) is not bool:
            raise VersionError('请明确选择声音、字幕和评论')
    if 'timeline' in raw:
        ref = raw['timeline']
        if ('partial' in raw or 'update' in raw or not isinstance(ref, dict) or set(ref) != {'plan_id'}
                or not isinstance(ref['plan_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', ref['plan_id'])):
            raise VersionError('成片剪辑方案无效，请从已生成成片重新建立。')
    if 'update' in raw:
        ref = raw['update']
        if ('partial' in raw or not isinstance(ref, dict) or set(ref) != {'plan_id'}
                or not isinstance(ref['plan_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', ref['plan_id'])):
            raise VersionError('所选内容更新方案无效，请从本项目的成片差异重新建立。')
    if 'partial' in raw:
        ref = raw['partial']
        if (not isinstance(ref, dict) or set(ref) != {'plan_id'}
                or not isinstance(ref['plan_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', ref['plan_id'])):
            raise VersionError('局部修订计划引用无效，请从本项目的已有成片重新建立。')
    overrides = raw.get('overrides', {})
    schema = {f['path']: f for f in fields()}
    if not isinstance(overrides, dict) or set(overrides) - set(schema):
        raise VersionError('版本只能覆盖展示与混音设置，不能覆盖识别、翻译或配音引擎')
    for key, value in overrides.items():
        f = schema[key]
        invalid = value is None
        if f['type'] == 'bool': invalid |= type(value) is not bool
        elif f['type'] in {'int', 'float'}:
            invalid |= (type(value) not in (int, float) or not math.isfinite(value)
                        or f['type'] == 'int' and int(value) != value
                        or f['min'] is not None and value < f['min'] or f['max'] is not None and value > f['max'])
        elif f['type'] == 'select':
            invalid |= not (isinstance(value, str) and 0 < len(value) <= 200) if f['editable'] else value not in [o['value'] for o in f['options']]
        elif f['type'] == 'color':
            from dubforge.pipeline.text_style import validate_color
            validate_color(value)
        if key in {'assemble.subtitle_font', 'assemble.subtitle_source_font', 'comments.font', 'comments.source_font'}:
            from dubforge.pipeline.text_style import validate_font
            validate_font(value)
        if invalid:
            raise VersionError(f'「{f["label"]}」的值无效；跟随默认请使用“恢复继承”')
    return {**copy.deepcopy(raw), 'name': raw['name'].strip(), 'overrides': copy.deepcopy(overrides)}


def validate_list(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 32:
        raise VersionError('请保留1–32个成片版本')
    result = [validate(v) for v in raw]
    if len({v['id'] for v in result}) != len(result):
        raise VersionError('版本标识不能重复；请使用“复制为新版本”')
    return result


def legacy_version(variant):
    return dict(id='legacy-' + variant, name=VARIANT_LABELS.get(variant, '原声原片'), **dimensions(variant), overrides={})


def catalog(uid):
    from dubforge import configuration
    from dubforge.project_execution import production_selection
    data = configuration.resolve(uid)
    return dict(versions=validate_list(data['values']['assembly.versions']), revision=data['revision'],
                target_version_ids=copy.deepcopy(data['values']['assembly.target_version_ids']),
                target_selection=production_selection(data['values']),
                fingerprint=data['fingerprint'], origin=data['origins']['assembly.versions'],
                source=data['sources']['assembly.versions'], sources=data['sources'],
                inherited='assembly.versions' not in data['overrides'])


def save(uid, body):
    versions = validate_list(body.get('versions'))
    if any(v.get('timeline') for v in versions):
        from dubforge.pipeline.assembly_timeline import load_plan as load_timeline
        from dubforge.pipeline.dependencies import Dependencies
        deps = Dependencies(crud.get_project(uid))
        for version in versions:
            if version.get('timeline'):
                load_timeline(deps, version)
    if any(v.get('update') for v in versions):
        from dubforge.pipeline.assembly_updates import load_plan as load_update
        from dubforge.pipeline.dependencies import Dependencies
        deps = Dependencies(crud.get_project(uid))
        for version in versions:
            if version.get('update'):
                load_update(deps, version)
    if any(v.get('partial') for v in versions):
        from dubforge.pipeline.assembly_partial import load_plan
        from dubforge.pipeline.dependencies import Dependencies
        deps = Dependencies(crud.get_project(uid))
        for version in versions:
            if version.get('partial'):
                load_plan(deps, version)
    from dubforge import configuration
    updates = {'assembly.versions': versions}
    if 'target_version_ids' in body:
        updates['assembly.target_version_ids'] = copy.deepcopy(body['target_version_ids'])
    configuration.save('project:' + uid, dict(updates=updates,
        revision=body.get('revision'), fingerprint=body.get('fingerprint')))
    crud.write_manifest(uid)
    return view(uid)


def base_values(deps, comment_settings=None):
    from dubforge import comment_studio, configuration
    if deps._assembly_configuration is None:
        deps._assembly_configuration = configuration.resolve(deps.project.uid, settings=deps.settings)
    data = deps._assembly_configuration
    comments = comment_settings if comment_settings is not None else {k: data['values']['comments.' + k] for k in comment_studio.DEFAULT_SETTINGS}
    values = {key: deps.cfg(key) for key in RENDER_KEYS}
    values.update({key: deps.cfg(key) for key in (
        'content_review.enabled', 'content_review.mute_audio', 'content_review.audio_mode',
        'content_review.blocked_terms', 'content_review.blocked_ranges')})
    allowed = {f['path'] for f in fields()}
    values.update({'comments.' + key: value for key, value in comments.items() if 'comments.' + key in allowed})
    origins = {key: data['origins'].get(key, '全局默认') for key in values}
    return values, origins


def resolve(deps, version, *, comment_plan=None, comment_settings=None, allow_stale_cuts=False):
    from dubforge.pipeline.video_encoding import profile, normalize_values, require_software_baseline
    if version.get('timeline'):
        from dubforge.pipeline.assembly_timeline import resolve as resolve_timeline
        resolved = resolve_timeline(deps, validate(version))
        require_software_baseline(resolved)
        resolved['values'] = normalize_values(resolved['values'])
        return resolved
    if version.get('update'):
        from dubforge.pipeline.assembly_updates import resolve as resolve_update
        resolved = resolve_update(deps, validate(version))
        require_software_baseline(resolved)
        resolved['values'] = normalize_values(resolved['values'])
        return resolved
    spec = validate(version)
    values, origins = base_values(deps, comment_settings)
    values.update(spec['overrides']); origins.update({k: '本版本' for k in spec['overrides']})
    if spec['burn_subtitles']:
        from dubforge.pipeline.subtitle_style import validate_style
        validate_style(values)
    overlay = None
    if spec['include_comments'] and comment_plan:
        from dubforge.comment_studio import validate_settings
        overlay = copy.deepcopy(comment_plan)
        overlay['settings'] = validate_settings({**overlay['settings'], **{k.removeprefix('comments.'): v for k, v in values.items() if k.startswith('comments.')}})
    from dubforge.pipeline.output_cuts import snapshot, plan_scope
    from dubforge.pipeline.audio_post import records as gain_records
    cuts_error = ''
    cut_scope = 'project'
    try:
        cuts = snapshot(deps, version_id=spec['id'])
        cut_scope = plan_scope(deps, spec['id'])
    except DubForgeError as exc:
        # 版本工作区需要在“源视频变更”时仍能展示参数和恢复入口；
        # 正式 plan/start 仍走严格路径并保留原错误码。
        if not allow_stale_cuts or not exc.code.startswith('OUTPUT_CUTS_'):
            raise
        cuts = None
        cuts_error = exc.message
    resolved = {**spec, 'values': values, 'origins': origins, 'comment_overlay': overlay,
                # 版本级剪辑方案优先；无版本方案时由 output_cuts.snapshot
                # 回退到旧项目级默认方案，保证旧项目继续可重建。
                'output_cuts': cuts,
                'output_cuts_scope': cut_scope,
                'output_cuts_error': cuts_error,
                'audio_line_gains': gain_records(deps.project) if spec['dubbing'] else {}}
    if spec.get('partial'):
        from dubforge.pipeline.assembly_partial import load_plan
        resolved['partial_plan'] = load_plan(deps, spec)
        baseline = resolved['partial_plan']['base_version']
        require_software_baseline(baseline)
        frozen = normalize_values(baseline['values'])
        if any(key in spec['overrides'] and spec['overrides'][key] != frozen[key] for key in (ENCODER_KEY, CQ_KEY)):
            raise VersionError('局部修订的视频编码方式与画质必须沿用基准；请完整生成普通版本后再更换编码。',
                               code='ASSEMBLY_PARTIAL_ENCODING')
        for key in (ENCODER_KEY, CQ_KEY):
            values[key] = frozen[key]
            origins[key] = '基准成片'
        if spec['dubbing'] != resolved['partial_plan']['base_version']['dubbing']:
            raise VersionError('局部修订保留基准的原声/配音类型；如需更换整片声音类型，请创建普通成片版本。',
                               code='ASSEMBLY_PARTIAL_AUDIO_KIND')
        if values['assemble.crf'] != resolved['partial_plan']['base_version']['values']['assemble.crf']:
            raise VersionError('局部修订的编码画质需与基准成片一致；更改画质请完整生成普通版本。',
                               code='ASSEMBLY_PARTIAL_ENCODING')
    resolved['encoder_profile'] = profile(values)
    return resolved


def requires_dubbing(version):
    """保留基准音轨的局部修订只消费画面/字幕，不要求重做旧配音。"""
    return bool(not version.get('update') and not version.get('timeline') and version['dubbing'] and (not version.get('partial')
                or version.get('partial_plan', {}).get('replace_audio', True)))


def selected(uid, ids):
    if not isinstance(ids, list) or not ids or any(not isinstance(k, str) for k in ids) or len(ids) != len(set(ids)):
        raise VersionError('请选择不重复的成片版本')
    rows = {v['id']: v for v in catalog(uid)['versions']}
    if set(ids) - set(rows): raise VersionError('成片版本已移除，请重新选择', code='ASSEMBLY_VERSION_CONFLICT')
    return [rows[key] for key in ids]


def active_values(version):
    from dubforge.pipeline.video_encoding import profile, normalize_values
    values = normalize_values(version['values'])
    keys = {'assemble.crf'} if version['burn_subtitles'] or version['include_comments'] or version.get('output_cuts') or version.get('partial') else set()
    if requires_dubbing(version):
        keys |= {'assemble.voice_volume', 'assemble.bgm_volume', 'assemble.loudnorm'}
        if values['assemble.bgm_volume'] > 0: keys.add('assemble.ducking')
        if values.get('assemble.sentence_leveling', False):
            keys |= {'assemble.sentence_leveling', 'assemble.sentence_leveling_max_db'}
    preserve_audio = version.get('partial') and not version.get('partial_plan', {}).get('replace_audio', True)
    if not preserve_audio and values.get('assemble.final_loudness_mode', 'off') != 'off':
        keys |= {'assemble.final_loudness_mode', 'assemble.final_loudness_lufs', 'assemble.final_true_peak_db'}
    if version['burn_subtitles']:
        keys |= {k for k in values if k.startswith(('assemble.subtitle_', 'subtitles.'))}
        if values['assemble.subtitle_track'] != 'bilingual': keys.discard('subtitles.source_first')
        if not values.get('assemble.subtitle_auto_fit', True):
            keys -= {'assemble.subtitle_min_size', 'assemble.subtitle_source_min_size',
                     'assemble.subtitle_shrink_width_percent'}
        if not values.get('assemble.subtitle_source_style_enabled') or values['assemble.subtitle_track'] == 'trans':
            keys = {key for key in keys if not key.startswith('assemble.subtitle_source_')}
    keys |= {k for k in values if k.startswith('content_review.')} if values.get('content_review.enabled', True) else {'content_review.enabled'}
    # 声音模式仅在实际屏蔽音频时消费，由指纹中的处理策略记录。
    keys.discard('content_review.audio_mode')
    if keys.intersection(ENCODING_KEYS):
        encoder = profile(values)
        if encoder['mode'] == 'nvenc':
            keys.discard('assemble.crf')
            keys |= {ENCODER_KEY, CQ_KEY}
    return {k: values[k] for k in keys}


def fingerprint(deps, version, *, legacy=False):
    """计算版本实际消费输入的指纹。

    ``legacy`` 只用于读取旧版登记的成片。旧版把评论方案的
    ``confirmed_at``（确认时间）写进了指纹；新方案已在
    :func:`comment_studio.render_fingerprint_plan` 中移除该非渲染字段。
    保留显式兼容开关，避免为了迁移历史记录而放宽正常的新鲜度判定。
    """
    if version.get('timeline'):
        from dubforge.pipeline.assembly_timeline import load_plan as load_timeline, RENDER_VERSION
        value = load_timeline(deps, version)
        return digest(dict(timeline=value['fingerprint'], renderer=RENDER_VERSION))
    if version.get('update'):
        from dubforge.pipeline.assembly_updates import load_plan as load_update
        value = load_update(deps, version)
        return digest(dict(update=value['fingerprint'], baseline=value['base_sha256']))
    scoped = deps.for_version(version)
    inputs = {f'{s}/{n}': scoped.arts.get(f'{s}/{n}') for s, n, _ in scoped.inputs('assemble')}
    data = dict(schema=1, dimensions={k: version[k] for k in ('dubbing', 'burn_subtitles', 'include_comments')},
                values=active_values(version), inputs={k: a.sha256 if a else 'missing' for k, a in inputs.items()})
    from dubforge.pipeline.video_encoding import profile, needs_encoding
    encoder = profile(version['values'])
    if needs_encoding(version) and encoder['mode'] == 'nvenc':
        data['video_encoder'] = encoder
    if version.get('output_cuts'):
        from dubforge.pipeline.output_cuts import RENDER_VERSION
        data['output_cuts'] = version['output_cuts']
        data['output_cuts_renderer'] = RENDER_VERSION
    if scoped.cfg('content_review.enabled', True):
        from dubforge.content_review import REVIEW_VERSION
        data['content_review'] = dict(version=REVIEW_VERSION, inputs={
            k: deps.arts[k].sha256 for k in ('transcribe/src.srt', 'split/split.txt', 'translate/translate.txt')
            if k in deps.arts})
    from dubforge.pipeline.subtitle_document import records
    if scoped.cfg('content_review.enabled', True):
        data['content_review']['edits'] = records(deps.project)
        from dubforge.pipeline.content_redaction import fingerprint_effect
        if (effect := fingerprint_effect(scoped)) is not None:
            data['content_review']['decisions'] = effect
        from dubforge.pipeline.content_redaction import audio_fingerprint
        if (audio := audio_fingerprint(scoped)) is not None:
            data['content_review']['audio_processing'] = audio
    keys = {'text', 'start', 'end'} if requires_dubbing(version) else set()
    if version['burn_subtitles']:
        keys |= {'start', 'end'}
        track = version['values']['assemble.subtitle_track']
        if track != 'trans': keys.add('source_text')
        if track != 'source': keys.add('text')
    data['edits'] = {k: {part: {f: v for f, v in row.get(part, {}).items() if f in keys} for part in ('base', 'values')}
                     for k, row in records(deps.project).items() if keys.intersection(row.get('values', {}))}
    if requires_dubbing(version):
        data['voices'] = [{k: l[k] for k in ('id', 'source', 'take')} for l in scoped.production_lines()]
        if version.get('audio_line_gains'):
            data['audio_line_gains'] = version['audio_line_gains']
        from dubforge.pipeline.voice_timing import settings, STRATEGY_VERSION
        from dubforge.pipeline.playback_timing import records as playback_records
        data['voice_placement'] = dict(strategy=STRATEGY_VERSION, settings=settings(scoped.cfg),
                                       manual=playback_records(deps.project))
    if version['include_comments']:
        from dubforge.comment_studio import render_fingerprint_plan
        from dubforge.pipeline.content_redaction import safe_overlay
        # 正式编码先筛查评论；新鲜度须比较同一实际方案，不能把保留的阅读原案算入成片。
        data['comments'] = render_fingerprint_plan(
            safe_overlay(scoped, version['comment_overlay']), legacy=legacy)
    if version.get('partial'):
        from dubforge.pipeline.assembly_partial import fingerprint as partial_fingerprint
        data['partial'] = partial_fingerprint(deps, version)
        if version['burn_subtitles']:
            from dubforge.pipeline.version_render import subtitle_lines
            data['subtitle_events'] = [dict(id=row.index, start=row.start, end=row.end, text=row.text)
                                       for row in subtitle_lines(scoped, version)]
    return digest(data)


def results(uid):
    with crud.session() as session:
        return {r.version_id: r for r in session.exec(select(AssemblyResult).where(AssemblyResult.project_uid == uid)).all()}


def _results(deps):
    """仅复用本次依赖读取中的版本登记；下次状态请求重新查询数据库。"""
    if deps._assembly_records is None:
        deps._assembly_records = results(deps.project.uid)
    return deps._assembly_records


def artifact_registered_at(artifact):
    """数据库历史无时区时间也按领域模型约定解释为 UTC。"""
    value = artifact.created_at
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat() if value else None


def result_metadata(record, artifact, *, current_name=None):
    """只描述匹配该文件身份的成功快照，不用当前设置推测制作时参数。"""
    if record.artifact_id != artifact.id or record.artifact_hash != artifact.sha256:
        return {}
    try:
        snapshot = json.loads(record.snapshot)
    except (ValueError, TypeError):
        return {}
    if not isinstance(snapshot, dict):
        return {}
    generated_at = None
    try:
        value = datetime.fromisoformat(snapshot.get('generated_at', ''))
        # 新登记的成功时间均带时区；不猜测旧快照里无时区文字的来源。
        if value.tzinfo is not None:
            generated_at = value.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        pass
    name = snapshot.get('name') if isinstance(snapshot.get('name'), str) else ''
    metadata = dict(version_id=record.version_id, version_name=current_name or name,
                    version_generated_name=name, generated_at=generated_at,
                    registered_at=artifact_registered_at(artifact),
                    generated_at_source='assembly_success' if generated_at else 'artifact_registered')
    if type(snapshot.get('dubbing')) is bool:
        metadata['has_dubbing'] = snapshot['dubbing']
    track = snapshot.get('values', {}).get('assemble.subtitle_track') if isinstance(snapshot.get('values'), dict) else None
    if (type(snapshot.get('dubbing')) is bool and type(snapshot.get('burn_subtitles')) is bool
            and type(snapshot.get('include_comments')) is bool
            and (not snapshot['burn_subtitles'] or track in {'source', 'trans', 'bilingual'})):
        label = ('配音' if snapshot['dubbing'] else '原声') + ' · ' + (
            {'source': '原文字幕', 'trans': '中文字幕', 'bilingual': '双语字幕'}[track] if snapshot['burn_subtitles'] else '无字幕')
        if snapshot['include_comments']: label += ' · 叠加评论'
        if snapshot.get('output_cuts'): label += ' · 已剪辑'
        if snapshot.get('review_render'): label += ' · 审查修订（复用旧文字与音轨）'
        if snapshot.get('partial'): label += ' · 局部修订（范围外保留基准成片）'
        if snapshot.get('update'): label += ' · 按所选改动更新'
        if snapshot.get('timeline'): label += ' · 成片剪辑（保留来源内容）'
        metadata['version_summary'] = label
    if 'output_cuts' in snapshot:
        metadata['output_cuts'] = snapshot['output_cuts']
    if isinstance(snapshot.get('video_encoding'), dict):
        metadata['video_encoding'] = copy.deepcopy(snapshot['video_encoding'])
    return metadata


def artifact_metadata(uid):
    saved = results(uid)
    if not saved: return {}
    names = {v['id']: v['name'] for v in catalog(uid)['versions']}
    artifacts = {a.id: a for a in crud.latest_artifacts(crud.get_project(uid).id, ['assemble']).values()}
    data = {}
    for row in saved.values():
        if row.artifact_id not in artifacts or artifacts[row.artifact_id].sha256 != row.artifact_hash:
            continue
        metadata = result_metadata(row, artifacts[row.artifact_id], current_name=names.get(row.version_id))
        if metadata:
            data[row.artifact_id] = metadata
    return data


def assembly_state(deps):
    """已有版本各自判定，不因新增一个尚未生成的版本让旧成品全部过期。"""
    saved = _results(deps)
    if not saved: return None
    from dubforge import comment_studio
    overlay = comment_studio.effective_overlay(deps.project.uid, comment_studio._read(deps.project).get('overlay'))
    current = [v for v in catalog(deps.project.uid)['versions'] if v['id'] in saved]
    if not current: return None
    try:
        outdated = [v['name'] for v in current if not is_current(deps, resolve(deps, v, comment_plan=overlay))]
    except DubForgeError as exc:
        if exc.code.startswith(('OUTPUT_CUTS_', 'ASSEMBLY_PARTIAL_')):
            return dict(fresh=False, label='局部修订需核对' if exc.code.startswith('ASSEMBLY_PARTIAL_') else '剪辑方案需核对', reason=exc.message)
        raise
    return dict(fresh=not outdated, label='部分版本需更新' if outdated else '已生成版本有效',
                reason='需更新：' + '；'.join(outdated) if outdated else '各版本的输入与生效设置未变化；分P名称与顺序不影响成品。')


def result_artifact(deps, version, records=None):
    record = (records if records is not None else _results(deps)).get(version['id'])
    if record:
        return next((a for a in deps.arts.values() if a.id == record.artifact_id), None)
    if version['id'].startswith('legacy-'):
        variant = version['id'].removeprefix('legacy-')
        matches = [a for a in deps.arts.values() if a.step_key == 'assemble' and final_variant(a.name) == variant]
        return max(matches, key=lambda a: a.id, default=None)
    return None


def is_current(deps, version, *, verify_hash=False):
    from dubforge.content_review import ContentReviewError
    from dubforge.pipeline.content_redaction import ContentRedactionError
    record = _results(deps).get(version['id'])
    art = result_artifact(deps, version, {version['id']: record} if record else {})
    if not record or not art or art.sha256 != record.artifact_hash: return False
    path = deps.path(art.step_key, art.name)
    try:
        current_fingerprint = fingerprint(deps, version)
        if record.fingerprint != current_fingerprint:
            # 兼容在确认时间参与指纹时生成的历史成片。历史快照保留了
            # 当时的确认时间，不能直接对当前方案重算 legacy 指纹；先比较
            # 新旧实际渲染摘要，再用历史快照的时间重算完整旧指纹。这样
            # 评论文字、顺序、样式、上游输入等任一真实依赖变化仍会失效。
            try:
                snapshot = json.loads(record.snapshot or '{}')
                legacy_version = copy.deepcopy(version)
                if version.get('include_comments'):
                    from dubforge.comment_studio import render_fingerprint_plan
                    from dubforge.pipeline.content_redaction import safe_overlay
                    saved_overlay = snapshot.get('comment_overlay')
                    current_overlay = safe_overlay(deps.for_version(version), version.get('comment_overlay'))
                    saved_overlay = safe_overlay(deps.for_version(version), saved_overlay)
                    if render_fingerprint_plan(current_overlay) != render_fingerprint_plan(saved_overlay):
                        return False
                    legacy_version['comment_overlay'] = snapshot.get('comment_overlay')
                if record.fingerprint != fingerprint(deps, legacy_version, legacy=True):
                    return False
            except (OSError, TypeError, ValueError, KeyError, AttributeError, ContentReviewError, ContentRedactionError):
                return False
        if not path or not path.is_file() or path.stat().st_size != art.size or deps.for_version(version).unready('assemble'): return False
        if verify_hash:
            with path.open('rb') as stream:
                return hashlib.file_digest(stream, 'sha256').hexdigest() == art.sha256
    except (OSError, DubForgeError):
        return False
    return True


def record_result(ctx, version, name, input_fingerprint):
    art = crud.latest_artifacts(ctx.project.id, ['assemble'])[f'assemble/{name}']
    # 运行期方案可从本项目的不可变引用读取，避免每次修订递归嵌套全部旧成片。
    snapshot = {key: value for key, value in version.items() if key not in {'update_plan', 'partial_plan', 'timeline_plan'}}
    snapshot['generated_at'] = datetime.now(timezone.utc).isoformat()
    with crud.session() as session:
        session.merge(AssemblyResult(key=f'{ctx.project.uid}:{version["id"]}', project_uid=ctx.project.uid,
            version_id=version['id'], artifact_id=art.id, artifact_hash=art.sha256, fingerprint=input_fingerprint,
            snapshot=json.dumps(snapshot, ensure_ascii=False)))
        session.commit()


def view(uid):
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge import comment_studio
    doc = catalog(uid)
    from dubforge.pipeline import assembly_inheritance
    deps = Dependencies(crud.get_project(uid))
    from dubforge.pipeline.output_cuts import view as cut_view, OutputCutsError
    cut_error = ''
    cut_catalog = {}
    try:
        # 不在这里缓存项目级方案：每个版本的 resolve() 必须独立读取
        # 自己的剪辑方案。此处仅用于向旧 UI 提供全局剪辑诊断。
        cut_catalog = cut_view(uid)
        # 源身份与时长在一次只读列表内共用；各版本的实际剪辑范围仍分别读取。
        deps._output_cuts_source_context = {key: cut_catalog.get(key) for key in
                                             ('source_fingerprint', 'duration', 'frame_rate')}
        if cut_catalog.get('source_changed'):
            cut_error = next(iter(cut_catalog.get('warnings') or ['源视频已变化，剪辑方案需核对。']))
    except OutputCutsError as exc:
        # 换源后的旧范围阻止制作，但不能把修复范围的编辑器一并挡在错误页外。
        cut_error = exc.message
    defaults, origins = base_values(deps)
    overlay = comment_studio.effective_overlay(uid, comment_studio._read(deps.project).get('overlay'))
    records = _results(deps)
    versions = []
    from dubforge.pipeline.version_render import VersionContext, uses_source_directly
    for spec in doc['versions']:
        partial_error = ''
        try:
            resolved = resolve(deps, spec, comment_plan=overlay, allow_stale_cuts=True)
        except DubForgeError as exc:
            if not spec.get('partial') or not exc.code.startswith('ASSEMBLY_PARTIAL_'):
                raise
            partial_error = exc.message
            resolved = resolve(deps, {k: v for k, v in spec.items() if k != 'partial'},
                               comment_plan=overlay, allow_stale_cuts=True)
        source_direct, source_error = False, ''
        # 旧项目级方案失效时，只阻断仍回退到项目方案的版本；已有
        # schema2 独立方案仍可读取、编辑和生成。
        version_cut_error = resolved.get('output_cuts_error') or (
            cut_error if resolved.get('output_cuts_scope', 'project') == 'project' else '')
        if partial_error:
            source_error = partial_error
        elif not version_cut_error:
            reuse_context = VersionContext(deps, resolved)
            try:
                source_direct = uses_source_directly(reuse_context, resolved)
                # 版本只允许覆盖渲染参数；审查设置与文字输入在本次读取内相同。
                # 仅原片直用候选会加载审查，后续候选共用一次文本筛查结果。
                if '_content_redaction_plan' in reuse_context.__dict__:
                    deps._content_redaction_plan = reuse_context._content_redaction_plan
            except DubForgeError as exc:
                source_error = exc.message
        art = result_artifact(deps, spec, records)
        record = records.get(spec['id'])
        reviewed = bool(record and json.loads(record.snapshot).get('review_render'))
        if version_cut_error or partial_error:
            current = False
        elif reviewed:
            from dubforge.pipeline.review_render import is_current as review_current
            current = bool(art and review_current(deps, resolved, record))
        else:
            current = is_current(deps, resolved) if art else False
        path = deps.path(art.step_key, art.name) if art else None
        exists = bool(path and path.is_file())
        from dubforge.pipeline.audio_quality import read_report
        versions.append({**spec, 'values': resolved['values'], 'origins': resolved['origins'],
                         'sources': assembly_inheritance.field_sources(spec, doc),
                         'artifact_id': art.id if art else None, 'fresh': current,
                         'uses_source_directly': source_direct, 'render_warning': source_error,
                         'output_cuts': resolved.get('output_cuts'),
                         'output_cuts_scope': resolved.get('output_cuts_scope', 'project'),
                         'cut_baseline': (cut_catalog.get('plans', {}).get(spec['id'], {}).get('baseline')
                                          if isinstance(cut_catalog.get('plans'), dict) else None),
                         'partial_summary': ({k: resolved['partial_plan'][k] for k in
                            ('base_name', 'ranges', 'replace_audio', 'duration', 'encode_seconds', 'summary')}
                            if resolved.get('partial_plan') else dict(summary=partial_error) if partial_error else None),
                         'update_summary': ({k: resolved['update_plan'][k] for k in
                            ('base_name', 'ranges', 'replace_audio', 'duration', 'encode_seconds', 'summary', 'method', 'selection_count', 'remaining_count')}
                            if resolved.get('update_plan') else None),
                         'timeline_summary': ({**{k: resolved['timeline_plan'][k] for k in
                            ('base_name', 'ranges', 'restored_ranges', 'source_duration', 'output_duration', 'summary')},
                            'encoding': resolved['timeline_plan'].get('encoding'),
                            'actual_encoding': (json.loads(record.snapshot).get('actual_encoding')
                                if record and json.loads(record.snapshot).get('timeline') == spec.get('timeline') else None)}
                            if resolved.get('timeline_plan') else None),
                         'audio_report': read_report(path) if exists else None,
                         'status': '局部修订需核对' if partial_error else '剪辑方案需核对' if version_cut_error else
                                   '审查设置需核对' if source_error else
                                   '原片直用' if source_direct else
                                   '审查修订有效（复用旧文字与音轨）' if reviewed and current else
                                   '所选改动已应用' if current and spec.get('update') else
                                   '已是最新' if current else '文件缺失' if art and not exists else '需更新' if art else '尚未生成'})
    return dict(**{**doc, 'versions': versions}, defaults=defaults, origins=origins, fields=list(fields()),
                # 旧成片基准清单与版本级方案摘要供剪辑编辑器选择；
                # 没有成功登记的产物不会出现在清单中。
                output_cuts_baselines=cut_catalog.get('baselines', []),
                output_cuts_plans=cut_catalog.get('plans', {}),
                output_cuts_error=cut_error,
                inheritance=assembly_inheritance.summary(uid, current=deps._assembly_configuration,
                    settings=deps.settings,
                    # 版本级剪辑方案独立计算；任一当前版本实际消费
                    # 剪辑时，继承摘要才标记为剪辑编码。
                    cut_encoding=any(bool(row.get('output_cuts')) for row in versions)))


def plan(uid, body, *, allow_draft=False):
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge import comment_studio
    project = crud.get_project(uid)
    if not project: raise VersionError('项目不存在', code='PROJECT_NOT_FOUND')
    force_rebuild = body.get('force_rebuild', False)
    if type(force_rebuild) is not bool:
        raise VersionError('请明确选择是否完整重新生成。')
    if allow_draft and force_rebuild:
        raise VersionError('试片不支持完整重制，请从正式生成入口建立新的完整成片。')
    specs = [validate(body['version'])] if allow_draft and 'version' in body else selected(uid, body.get('version_ids'))
    if force_rebuild and any(version.get('partial') or version.get('update') or version.get('timeline') for version in specs):
        raise VersionError('完整更新需先建立普通成片版本；局部修订版本只更新其固定范围。',
                           code='ASSEMBLY_FORCE_FULL_ONLY')
    deps = Dependencies(project)
    needs_comments = any(v['include_comments'] and not v.get('update') and not v.get('timeline') for v in specs)
    overlay = comment_studio.for_task(uid, needs_comments, body.get('overlay_revision'))
    versions = [resolve(deps, v, comment_plan=overlay) for v in specs]
    if allow_draft and any(v.get('partial') or v.get('timeline') for v in versions):
        raise VersionError('局部修订请生成完整的新成片后预览，试片不能代替保留基准的局部成片。')
    dependencies = deps.with_versions(versions)
    blockers = dependencies.unready('assemble')
    if not blockers:
        # 验证当前校对稿和评论语言，不要等到编码之后才发现缺译文。
        from dubforge.pipeline.subtitle_document import read
        from dubforge.comment_studio import display_lines
        for version in versions:
            if version.get('timeline'):
                from dubforge.pipeline.assembly_timeline import load_plan as load_timeline
                load_timeline(deps, version, verify=True)
                continue
            if version.get('update'):
                from dubforge.pipeline.assembly_updates import load_plan as load_update
                load_update(deps, version, verify=True)
                continue
            if version['burn_subtitles']:
                read(deps.for_version(version), translation=version['values']['assemble.subtitle_track'] != 'source')
            for item in (version['comment_overlay'] or {}).get('items', []):
                display_lines(item, version['comment_overlay']['settings'])
    inputs = [dict(id=v['id'], name=v['name'], fingerprint=fingerprint(deps, v)) for v in versions]
    # 默认入口沿用既有确认合同；显式重制属于本次运行范围，必须单独确认。
    stamp = digest(dict(versions=inputs, force_rebuild=True) if force_rebuild else inputs)
    return dict(versions=versions, blockers=blockers, fingerprint=stamp, force_rebuild=force_rebuild,
                replacements=[v['name'] for v in versions if result_artifact(deps, v)],
                reusable=[] if force_rebuild else [v['id'] for v in versions if is_current(deps, v)],
                comments_count=len(overlay['items']) if overlay else 0)
