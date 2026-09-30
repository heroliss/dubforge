"""成片版本的单一入口：身份、显式继承、冻结、依赖、指纹与产物归属。"""
import copy
import hashlib
import json
import math
import re
from dataclasses import asdict

from sqlmodel import select

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.assembly_models import AssemblyResult
from dubforge.pipeline.assembly_outputs import dimensions, final_variant, VARIANT_LABELS
from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA, ParamField, source_style_fields


class VersionError(DubForgeError):
    code = 'ASSEMBLY_VERSION_INVALID'
    suggestion = '请检查成片版本设置后重试；不会修改其他版本或已发布稿件。'


ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')
RENDER_KEYS = {'assemble.voice_volume', 'assemble.bgm_volume', 'assemble.ducking', 'assemble.loudnorm', 'assemble.crf',
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
                   help='0 为自动。数值是占整个画面的宽度百分比，受边距限制；短评论不会强行撑满。'),
        ParamField('comments.position', '位置', 'select', options=tuple((label, value) for label, value in
            [('左上', 'top_left'), ('顶部居中', 'top'), ('右上', 'top_right'), ('左下', 'bottom_left'), ('底部居中', 'bottom'), ('右下', 'bottom_right')])),
        ParamField('comments.font', '译文字体（通用）', 'select', editable=True, options=(('微软雅黑', 'Microsoft YaHei'), ('Arial', 'Arial'))),
        ParamField('comments.font_size', '译文字号（通用）', 'int', min=18, max=72, step=1),
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
                         '评论' if item['path'].startswith('comments.') else '输出' if item['path'] == 'assemble.crf' else '声音')
        yield item


def validate(raw):
    if not isinstance(raw, dict) or set(raw) - {'id', 'name', 'dubbing', 'burn_subtitles', 'include_comments', 'overrides'}:
        raise VersionError('成片版本格式无效')
    if not isinstance(raw.get('id'), str) or not ID.fullmatch(raw['id']):
        raise VersionError('成片版本标识无效')
    if not isinstance(raw.get('name'), str) or not 1 <= len(raw['name'].strip()) <= 80:
        raise VersionError('请填写1–80字的版本名称')
    for key in ('dubbing', 'burn_subtitles', 'include_comments'):
        if type(raw.get(key)) is not bool:
            raise VersionError('请明确选择声音、字幕和评论')
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
    data = configuration.resolve(uid)
    return dict(versions=validate_list(data['values']['assembly.versions']), revision=data['revision'],
                fingerprint=data['fingerprint'], origin=data['origins']['assembly.versions'],
                source=data['sources']['assembly.versions'], sources=data['sources'],
                inherited='assembly.versions' not in data['overrides'])


def save(uid, body):
    versions = validate_list(body.get('versions'))
    from dubforge import configuration
    configuration.save('project:' + uid, dict(updates={'assembly.versions': versions},
        revision=body.get('revision'), fingerprint=body.get('fingerprint')))
    crud.write_manifest(uid)
    return view(uid)


def base_values(deps, comment_settings=None):
    from dubforge import comment_studio, configuration
    data = configuration.resolve(deps.project.uid)
    comments = comment_settings if comment_settings is not None else {k: data['values']['comments.' + k] for k in comment_studio.DEFAULT_SETTINGS}
    values = {key: deps.cfg(key) for key in RENDER_KEYS}
    values.update({'comments.' + key: value for key, value in comments.items() if any(f['path'] == 'comments.' + key for f in fields())})
    origins = {key: data['origins'].get(key, '全局默认') for key in values}
    return values, origins


def resolve(deps, version, *, comment_plan=None, comment_settings=None):
    spec = validate(version)
    values, origins = base_values(deps, comment_settings)
    values.update(spec['overrides']); origins.update({k: '本版本' for k in spec['overrides']})
    overlay = None
    if spec['include_comments'] and comment_plan:
        from dubforge.comment_studio import validate_settings
        overlay = copy.deepcopy(comment_plan)
        overlay['settings'] = validate_settings({**overlay['settings'], **{k.removeprefix('comments.'): v for k, v in values.items() if k.startswith('comments.')}})
    return {**spec, 'values': values, 'origins': origins, 'comment_overlay': overlay}


def selected(uid, ids):
    if not isinstance(ids, list) or not ids or any(not isinstance(k, str) for k in ids) or len(ids) != len(set(ids)):
        raise VersionError('请选择不重复的成片版本')
    rows = {v['id']: v for v in catalog(uid)['versions']}
    if set(ids) - set(rows): raise VersionError('成片版本已移除，请重新选择', code='ASSEMBLY_VERSION_CONFLICT')
    return [rows[key] for key in ids]


def active_values(version):
    values = version['values']
    keys = {'assemble.crf'} if version['burn_subtitles'] or version['include_comments'] else set()
    if version['dubbing']:
        keys |= {'assemble.voice_volume', 'assemble.bgm_volume', 'assemble.loudnorm'}
        if values['assemble.bgm_volume'] > 0: keys.add('assemble.ducking')
    if version['burn_subtitles']:
        keys |= {k for k in values if k.startswith(('assemble.subtitle_', 'subtitles.'))}
        if values['assemble.subtitle_track'] != 'bilingual': keys.discard('subtitles.source_first')
        if not values.get('assemble.subtitle_source_style_enabled') or values['assemble.subtitle_track'] == 'trans':
            keys = {key for key in keys if not key.startswith('assemble.subtitle_source_')}
    return {k: values[k] for k in keys}


def fingerprint(deps, version):
    scoped = deps.for_version(version)
    inputs = {f'{s}/{n}': scoped.arts.get(f'{s}/{n}') for s, n, _ in scoped.inputs('assemble')}
    data = dict(schema=1, dimensions={k: version[k] for k in ('dubbing', 'burn_subtitles', 'include_comments')},
                values=active_values(version), inputs={k: a.sha256 if a else 'missing' for k, a in inputs.items()})
    from dubforge.pipeline.subtitle_document import records
    keys = {'text', 'start', 'end'} if version['dubbing'] else set()
    if version['burn_subtitles']:
        keys |= {'start', 'end'}
        track = version['values']['assemble.subtitle_track']
        if track != 'trans': keys.add('source_text')
        if track != 'source': keys.add('text')
    data['edits'] = {k: {part: {f: v for f, v in row.get(part, {}).items() if f in keys} for part in ('base', 'values')}
                     for k, row in records(deps.project).items() if keys.intersection(row.get('values', {}))}
    if version['dubbing']:
        from dubforge.pipeline.dubbing_edit import production_lines
        data['voices'] = [{k: l[k] for k in ('id', 'source', 'take')} for l in production_lines(deps.project, scoped.dubbing_lines(), scoped.root, dependencies=scoped)]
    if version['include_comments']:
        from dubforge.comment_studio import render_fingerprint_plan
        data['comments'] = render_fingerprint_plan(version['comment_overlay'])
    return digest(data)


def results(uid):
    with crud.session() as session:
        return {r.version_id: r for r in session.exec(select(AssemblyResult).where(AssemblyResult.project_uid == uid)).all()}


def artifact_metadata(uid):
    saved = results(uid)
    if not saved: return {}
    names = {v['id']: v['name'] for v in catalog(uid)['versions']}
    artifacts = {a.id: a for a in crud.latest_artifacts(crud.get_project(uid).id, ['assemble']).values()}
    data = {}
    for row in saved.values():
        if row.artifact_id not in artifacts or artifacts[row.artifact_id].sha256 != row.artifact_hash:
            continue
        snapshot = json.loads(row.snapshot)
        track = snapshot['values']['assemble.subtitle_track']
        label = ('配音' if snapshot['dubbing'] else '原声') + ' · ' + (
            {'source': '原文字幕', 'trans': '中文字幕', 'bilingual': '双语字幕'}[track] if snapshot['burn_subtitles'] else '无字幕')
        if snapshot['include_comments']: label += ' · 叠加评论'
        data[row.artifact_id] = dict(version_id=row.version_id, version_name=names.get(row.version_id, snapshot['name']),
                                     version_summary=label, has_dubbing=snapshot['dubbing'])
    return data


def assembly_state(deps):
    """已有版本各自判定，不因新增一个尚未生成的版本让旧成品全部过期。"""
    saved = results(deps.project.uid)
    if not saved: return None
    from dubforge import comment_studio
    overlay = comment_studio.effective_overlay(deps.project.uid, comment_studio._read(deps.project).get('overlay'))
    current = [v for v in catalog(deps.project.uid)['versions'] if v['id'] in saved]
    if not current: return None
    outdated = [v['name'] for v in current if not is_current(deps, resolve(deps, v, comment_plan=overlay))]
    return dict(fresh=not outdated, label='部分版本需更新' if outdated else '已生成版本有效',
                reason='需更新：' + '；'.join(outdated) if outdated else '各版本的输入与生效设置未变化；分P名称与顺序不影响成品。')


def result_artifact(deps, version, records=None):
    record = (records if records is not None else results(deps.project.uid)).get(version['id'])
    if record:
        return next((a for a in deps.arts.values() if a.id == record.artifact_id), None)
    if version['id'].startswith('legacy-'):
        variant = version['id'].removeprefix('legacy-')
        matches = [a for a in deps.arts.values() if a.step_key == 'assemble' and final_variant(a.name) == variant]
        return max(matches, key=lambda a: a.id, default=None)
    return None


def is_current(deps, version, *, verify_hash=False):
    record = results(deps.project.uid).get(version['id'])
    art = result_artifact(deps, version, {version['id']: record} if record else {})
    if not record or not art or art.sha256 != record.artifact_hash or record.fingerprint != fingerprint(deps, version): return False
    path = deps.path(art.step_key, art.name)
    try:
        if not path or not path.is_file() or path.stat().st_size != art.size or deps.for_version(version).unready('assemble'): return False
        if verify_hash:
            with path.open('rb') as stream:
                return hashlib.file_digest(stream, 'sha256').hexdigest() == art.sha256
    except OSError:
        return False
    return True


def record_result(ctx, version, name, input_fingerprint):
    art = crud.latest_artifacts(ctx.project.id, ['assemble'])[f'assemble/{name}']
    with crud.session() as session:
        session.merge(AssemblyResult(key=f'{ctx.project.uid}:{version["id"]}', project_uid=ctx.project.uid,
            version_id=version['id'], artifact_id=art.id, artifact_hash=art.sha256, fingerprint=input_fingerprint,
            snapshot=json.dumps(version, ensure_ascii=False)))
        session.commit()


def view(uid):
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge import comment_studio
    doc = catalog(uid)
    deps = Dependencies(crud.get_project(uid))
    defaults, origins = base_values(deps)
    overlay = comment_studio.effective_overlay(uid, comment_studio._read(deps.project).get('overlay'))
    records = results(uid)
    versions = []
    for spec in doc['versions']:
        resolved = resolve(deps, spec, comment_plan=overlay)
        art = result_artifact(deps, spec, records)
        current = is_current(deps, resolved) if art else False
        path = deps.path(art.step_key, art.name) if art else None
        exists = bool(path and path.is_file())
        versions.append({**spec, 'values': resolved['values'], 'origins': resolved['origins'],
                         'artifact_id': art.id if art else None, 'fresh': current,
                         'status': '原片直用' if not any(spec[k] for k in ('dubbing','burn_subtitles','include_comments')) else
                                   '已是最新' if current else '文件缺失' if art and not exists else '需更新' if art else '尚未生成'})
    return dict(**{**doc, 'versions': versions}, defaults=defaults, origins=origins, fields=list(fields()))


def plan(uid, body, *, allow_draft=False):
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge import comment_studio
    project = crud.get_project(uid)
    if not project: raise VersionError('项目不存在', code='PROJECT_NOT_FOUND')
    specs = [validate(body['version'])] if allow_draft and 'version' in body else selected(uid, body.get('version_ids'))
    deps = Dependencies(project)
    needs_comments = any(v['include_comments'] for v in specs)
    overlay = comment_studio.for_task(uid, needs_comments, body.get('overlay_revision'))
    versions = [resolve(deps, v, comment_plan=overlay) for v in specs]
    dependencies = deps.with_versions(versions)
    blockers = dependencies.unready('assemble')
    if not blockers:
        # 验证当前校对稿和评论语言，不要等到编码之后才发现缺译文。
        from dubforge.pipeline.subtitle_document import read
        from dubforge.comment_studio import display_lines
        for version in versions:
            if version['burn_subtitles']:
                read(deps.for_version(version), translation=version['values']['assemble.subtitle_track'] != 'source')
            for item in (version['comment_overlay'] or {}).get('items', []):
                display_lines(item, version['comment_overlay']['settings'])
    stamp = digest([dict(id=v['id'], name=v['name'], fingerprint=fingerprint(deps, v)) for v in versions])
    return dict(versions=versions, blockers=blockers, fingerprint=stamp,
                replacements=[v['name'] for v in versions if result_artifact(deps, v)],
                reusable=[v['id'] for v in versions if is_current(deps, v)],
                comments_count=len(overlay['items']) if overlay else 0)
