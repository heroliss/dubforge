"""输入差异的唯一说明入口；只读、脱敏，不用当前值伪造历史值。"""
import copy
import datetime
import hashlib
import json
import re
from urllib.parse import urlsplit

from dubforge.errors import DubForgeError
from dubforge.pipeline.registry import STEP_MAP

SCHEMA = 1
_SECRET = re.compile(r'sk-[A-Za-z0-9_-]{6,}|gh[pousr]_[A-Za-z0-9_]{6,}|[0-9a-fA-F]{32}\.[A-Za-z0-9]{8}')
_URL = re.compile(r'https?://[^\s<>"\']+', re.I)
_PRIVATE = re.compile(r'(^|[._/])(api_key|password|credential|authorization|cookies?|proxy|secret|token)([._/]|$)', re.I)
_PROMPT_LABELS = {'correction_prompt': '转录纠错与语义整理提示词版本',
                  'semantic_segmentation': '语义分句质量规则版本',
                  'translation_windows': '翻译分块与衔接策略版本',
                  'translation_prompt': '翻译提示词版本', 'content_review.version': '内容筛查规则版本'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _safe_text(value, limit=200):
    text = _SECRET.sub('（已脱敏）', str(value))
    text = re.sub(r'(?i)(?:api[_ -]?key|password|credential|authorization|cookies?|secret|token)\s*[:=]\s*[^\s,;]+|Bearer\s+[^\s,;]+', '（已脱敏）', text)
    text = _URL.sub('（地址已脱敏）', text)
    # 用户定义名称也不能把绝对路径带到历史快照或明细弹窗。
    text = re.sub(r'[A-Za-z]:[\\/][^\s，；]+|(?<![\w])/(?:[^/\s]+/)+[^\s，；]+', '（路径已脱敏）', text)
    return text[:limit] + ('…' if len(text) > limit else '')


def _fields():
    from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA
    return {f.path: f for group in STEP_PARAM_SCHEMA.values() for f in group
            if f.type != 'password' and not _PRIVATE.search(f.path)
            and not f.path.endswith(('.url', '.project_dir'))}


def _origin(path, *, current=False, deps=None):
    if not current:
        return dict(kind='execution', label='生成时生效设置')
    from dubforge import configuration
    data = getattr(deps, '_explanation_origins', None)
    if data is None:
        data = configuration.resolve(deps.project.uid, settings=deps.settings)
        deps._explanation_origins = data
    if path in data['values'] and data['values'][path] == deps.cfg(path):
        source = data['sources'].get(path, {})
        return dict(kind=source.get('kind', 'effective'), label=_safe_text(data['origins'].get(path, '当前生效设置')))
    return dict(kind='effective', label='当前生效设置')


def _add(entries, key, label, value, category='parameter', *, hashed=False, origin=None, display=None):
    if _PRIVATE.search(key):
        return
    if hashed:
        stored = _digest(value)
        display = display or f'内容摘要 {stored[:12]}'
    elif isinstance(value, str):
        stored = _safe_text(value, 200)
        display = display if display is not None else (stored or '（空）')
    elif value is None or type(value) in (int, float, bool):
        stored = value
        display = display if display is not None else ('是' if value is True else '否' if value is False else '（未设置）' if value is None else str(value))
    else:
        stored = _digest(value)
        display = display or f'内容摘要 {stored[:12]}'
    entries[_safe_text(key)] = dict(category=category, label=_safe_text(label), value=stored,
        display=_safe_text(display), origin=origin or dict(kind='input', label='生成输入'))


def _flatten(raw, prefix=''):
    for key, value in raw.items():
        path = prefix + '.' + key if prefix else key
        if isinstance(value, dict):
            yield from _flatten(value, path)
        else:
            yield path, value


def _parameter_entries(entries, raw, deps, *, prefix='', label_prefix='', current=False, origins=None):
    allowed = _fields()
    for path, value in _flatten(raw):
        field = allowed.get(path)
        if not field:
            continue
        if origins is not None:
            origin = dict(kind='version' if origins.get(path) == '本版本' else 'effective',
                          label=_safe_text(origins.get(path, '生成时生效设置')) if current else '生成时生效设置')
        else:
            origin = _origin(path, current=current, deps=deps)
        category = ('prompt' if 'prompt' in path else 'glossary' if 'custom_terms' in path else
                    'model' if path.endswith(('.model', '.backend', '.llm_profile')) else 'parameter')
        private_value = field.type == 'text' or path.endswith(('_path', '_audio'))
        _add(entries, prefix + path, label_prefix + field.label, value, category,
             hashed=private_value, origin=origin,
             display=('已选择文件；内容或选择变化时需更新' if path.endswith(('_path', '_audio')) and value else None))


def _input_entries(entries, inputs, deps, *, prefix='', label_prefix=''):
    descriptions = {f'{p}/{n}': desc for p, n, desc in deps.inputs(deps._explanation_step)}
    for item in inputs:
        name, sha = item.rsplit(':', 1)
        producer = name.split('/', 1)[0]
        label = label_prefix + (descriptions.get(name) or name.split('/', 1)[-1])
        category = 'text' if producer in {'transcribe', 'split', 'translate', 'subtitles'} else 'upstream'
        _add(entries, prefix + 'input.' + name, label, sha, category,
             display='尚未生成' if sha == 'missing' else f'文件摘要 {sha[:12]}')


def _payload_entries(deps, key, payload, *, current=False):
    entries = {}
    cfg = payload.get('cfg', {})
    _parameter_entries(entries, cfg, deps, current=current)
    # 固定启用顺序与选中组来自真正参与指纹的模型链；不保留密钥、任意请求头或地址。
    profiles = cfg.get('llm', {}).get('profiles', []) if isinstance(cfg.get('llm'), dict) else []
    for index, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            continue
        origin = dict(kind='effective' if current else 'execution', label='全局模型配置' if current else '生成时生效设置')
        for name, label in [('model', '模型'), ('temperature', '随机程度'), ('top_p', '采样范围'), ('max_tokens', '最大输出长度')]:
            if name in profile:
                _add(entries, f'llm.profile.{index}.{name}', f'第{index + 1}可用模型 · {label}', profile[name], 'model', origin=origin)
        endpoint = profile.get('base_url')
        if endpoint:
            try:
                parsed = urlsplit(str(endpoint))
                # 对比服务身份，凭据、URL查询与片段既不保存也不参与这里的差异。
                identity = [parsed.scheme, parsed.hostname, parsed.port, parsed.path]
            except ValueError:
                identity = '不可解析的服务入口'
            _add(entries, f'llm.profile.{index}.provider', f'第{index + 1}可用模型 · 服务入口', identity,
                 'model', hashed=True, origin=origin, display=f'服务入口摘要 {_digest(identity)[:12]}')
    if 'custom_terms_file' in cfg:
        sha = cfg['custom_terms_file']
        _add(entries, 'glossary.contents', '术语表文件内容', sha, 'glossary',
             display='术语表文件无法读取' if sha == 'unavailable' else f'文件摘要 {str(sha)[:12]}')
    for name, label, category in [('reference_files', '参考音频文件版本', 'parameter'),
                                  ('line_settings', '逐句配音设置', 'parameter'),
                                  ('platform_subtitle', '已确认的平台字幕', 'text')]:
        if name in cfg:
            _add(entries, 'input_state.' + name, label, cfg[name], category, hashed=True)
    for name, label in _PROMPT_LABELS.items():
        raw = cfg if name == 'content_review.version' else payload
        if name in raw:
            _add(entries, 'rule.' + name, label, raw[name], 'prompt')
    for name, label, category in [('subtitle_edits', '字幕校对文字与时间轴', 'text'),
                                  ('content_review_edits', '内容筛查使用的校对记录', 'text'),
                                  ('dubbing_edit', '成片声源与配音版本选择', 'parameter')]:
        if name in payload:
            _add(entries, 'text.' + name, label, payload[name], category, hashed=True)
    deps._explanation_step = key
    _input_entries(entries, payload.get('upstream', []), deps)
    source = payload.get('source')
    if isinstance(source, dict):
        for name, label in [('kind', '视频来源类型'), ('size', '源视频大小'), ('mtime_ns', '源视频文件版本'), ('missing', '源视频是否缺失')]:
            if name in source:
                _add(entries, 'source.' + name, label, source[name], 'upstream')
        if 'url' in source:
            _add(entries, 'source.identity', '选中的源视频', source['url'], 'upstream', hashed=True)
    return entries


def _version_entries(deps, versions, *, current=False):
    from dubforge import comment_studio
    from dubforge.pipeline import assembly_versions as av
    from dubforge.pipeline.subtitle_document import records
    from dubforge.pipeline.dubbing_edit import production_lines
    entries = {}
    for version in versions:
        scoped = deps.for_version(version)
        scoped._explanation_step = 'assemble'
        prefix = 'version.' + version['id'] + '.'
        label = _safe_text(version['name']) + ' · '
        _parameter_entries(entries, av.active_values(version), scoped, prefix=prefix, label_prefix=label,
                           current=current, origins=version.get('origins', {}))
        for dimension, title in [('dubbing', '配音'), ('burn_subtitles', '烧录字幕'), ('include_comments', '叠加评论')]:
            _add(entries, prefix + dimension, label + title, version[dimension])
        _input_entries(entries, [f'{p}/{n}:{scoped.arts[f"{p}/{n}"].sha256 if f"{p}/{n}" in scoped.arts else "missing"}'
                               for p, n, _ in scoped.inputs('assemble')], scoped, prefix=prefix, label_prefix=label)
        keys = {'text', 'start', 'end'} if version['dubbing'] else set()
        if version['burn_subtitles']:
            keys |= {'start', 'end'}
            track = version['values']['assemble.subtitle_track']
            if track != 'trans': keys.add('source_text')
            if track != 'source': keys.add('text')
        edits = {k: {part: {f: v for f, v in row.get(part, {}).items() if f in keys} for part in ('base', 'values')}
                 for k, row in records(deps.project).items() if keys.intersection(row.get('values', {}))}
        if keys:
            _add(entries, prefix + 'text.edits', label + '字幕校对文字与时间轴', edits, 'text', hashed=True)
        if scoped.cfg('content_review.enabled', True):
            _add(entries, prefix + 'text.review_edits', label + '内容筛查使用的校对记录', records(deps.project), 'text', hashed=True)
            _input_entries(entries, [f'{name}:{deps.arts[name].sha256}' for name in
                ('transcribe/src.srt', 'split/split.txt', 'translate/translate.txt') if name in deps.arts],
                scoped, prefix=prefix, label_prefix=label)
        if version['dubbing']:
            voices = [{k: line[k] for k in ('id', 'source', 'take')}
                      for line in production_lines(deps.project, scoped.dubbing_lines(), scoped.root, dependencies=scoped)]
            _add(entries, prefix + 'voices', label + '成片声源与配音版本选择', voices, hashed=True)
        if version['include_comments']:
            overlay = comment_studio.render_fingerprint_plan(version.get('comment_overlay'))
            _add(entries, prefix + 'text.comments', label + '确认的评论内容与布局', overlay, 'text', hashed=True)
    return entries


def capture(deps, key, *, fingerprint=None, scope='full', incomplete_comments=False, _current=False):
    """执行开始时捕获安全证据；调用方仅在正式制作成功后写入基线。"""
    if key not in STEP_MAP:
        raise DubForgeError('制作步骤不存在', code='STEP_UNKNOWN')
    if scope in {'all', 'default'}:
        scope = 'full'
    from dubforge.runner import fingerprint_payload, compute_fingerprint
    if scope == 'versions' and key == 'assemble' and deps.assembly_versions:
        from dubforge.pipeline import assembly_versions as av
        entries = _version_entries(deps, deps.assembly_versions, current=_current)
        actual = 'versions:' + av.digest([av.fingerprint(deps, v) for v in deps.assembly_versions])
        selection = {'version_ids': [v['id'] for v in deps.assembly_versions]}
        incomplete_comments |= any(v['include_comments'] and not v.get('comment_overlay') for v in deps.assembly_versions)
    else:
        entries = _payload_entries(deps, key, fingerprint_payload(deps.project, key, dependencies=deps), current=_current)
        actual = compute_fingerprint(deps.project, key, dependencies=deps)
        if scope == 'outputs' and fingerprint and fingerprint.startswith('outputs:'):
            actual = 'outputs:' + actual
        selection = {'variants': deps.assemble_variants} if scope == 'outputs' else {}
        incomplete_comments |= scope == 'outputs' and any(v.endswith('_comments') for v in deps.assemble_variants or [])
    return dict(schema=SCHEMA, step=key, fingerprint=fingerprint if fingerprint is not None else actual,
        captured_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), scope=scope,
        selection=selection, entries=entries, incomplete_comments=bool(incomplete_comments))


def _history(deps, key, stored):
    step = deps.steps[key]
    if stored is None:
        stored = getattr(step, 'input_snapshot', '')
    try:
        stored = json.loads(stored) if isinstance(stored, str) else copy.deepcopy(stored)
        if (not isinstance(stored, dict) or stored.get('schema') != SCHEMA or stored.get('step') != key
                or not isinstance(stored.get('entries'), dict) or not stored.get('fingerprint')
                or stored.get('scope') not in {'full', 'outputs', 'versions'} or stored.get('incomplete_comments') is True):
            return None, deps
        # 失败的新尝试可改变StepRun指纹；旧快照仍解释上次成功输入，但不能证明文件完整。
        if step.status not in {'failed', 'interrupted', 'running', 'queued'} and stored['fingerprint'] != step.input_fingerprint:
            return None, deps
        scoped = deps
        selection = stored.get('selection', {})
        if stored['scope'] == 'versions':
            from dubforge import comment_studio
            from dubforge.pipeline import assembly_versions as av
            definitions = av.selected(deps.project.uid, selection['version_ids'])
            overlay = comment_studio.effective_overlay(deps.project.uid, comment_studio._read(deps.project).get('overlay'))
            resolved = [av.resolve(deps, v, comment_plan=overlay) for v in definitions]
            from dubforge.pipeline.version_render import VersionContext
            from dubforge.pipeline.content_redaction import safe_overlay
            for version in resolved:
                if version['include_comments'] and version.get('comment_overlay'):
                    version['comment_overlay'] = safe_overlay(VersionContext(deps, version), version['comment_overlay'])
            scoped = deps.with_versions(resolved)
        elif stored['scope'] == 'outputs':
            if key != 'assemble' or not selection.get('variants'):
                return None, deps
            scoped = deps.with_assembly_variants(selection['variants'])
        return stored, scoped
    except (DubForgeError, KeyError, TypeError, ValueError):
        return None, deps


def _change(key, label, category, *, old=None, new=None, message='', upstream_step=None):
    def origin(entry):
        value = entry.get('origin') if entry else None
        kinds = {'execution', 'effective', 'global', 'project', 'series', 'version', 'input'}
        return dict(kind=value['kind'], label=_safe_text(value.get('label', '生效设置'))) if isinstance(value, dict) and value.get('kind') in kinds else None
    result = dict(category=category if category in {'parameter', 'model', 'prompt', 'glossary', 'text', 'upstream', 'missing', 'unknown'} else 'unknown', key=_safe_text(key), label=_safe_text(label),
        before=_safe_text(old.get('display', '')) if old else None,
        after=_safe_text(new.get('display', '')) if new else None, before_known=old is not None,
        origin_before=origin(old), origin_after=origin(new),
        message=_safe_text(message, 350))
    if upstream_step:
        result['upstream_step'] = upstream_step
    return result


def _valid_key(name):
    """历史记录可被手工恢复；只接受本模块确实生成过的公开字段。"""
    if not isinstance(name, str) or _PRIVATE.search(name):
        return False
    if name in _fields():
        return True
    if re.fullmatch(r'llm\.profile\.\d+\.(model|temperature|top_p|max_tokens|provider)', name):
        return True
    if name in {'glossary.contents', 'input_state.reference_files', 'input_state.line_settings', 'input_state.platform_subtitle',
                'text.subtitle_edits', 'text.content_review_edits', 'text.dubbing_edit',
                'source.kind', 'source.size', 'source.mtime_ns', 'source.missing', 'source.identity'}:
        return True
    if name in {'rule.' + key for key in _PROMPT_LABELS}:
        return True
    if name.startswith('input.'):
        producer, _, filename = name.removeprefix('input.').partition('/')
        known = {n for step in STEP_MAP.values() for p, n, _ in step.inputs if p == producer}
        return producer in STEP_MAP and (filename in known or producer == 'tts' and bool(re.fullmatch(r'\d+(?:_v\d+)?\.wav', filename)))
    match = re.fullmatch(r'version\.([a-z0-9][a-z0-9_-]{0,63})\.(.+)', name)
    if match:
        child = match[2]
        return child in {'dubbing', 'burn_subtitles', 'include_comments', 'voices', 'text.edits', 'text.review_edits', 'text.comments'} or _valid_key(child)
    return False


def _safe_previous(name, entry):
    """摘要类输入绝不信任恢复记录的展示正文或路径。"""
    if not isinstance(entry, dict):
        return None
    child = name
    match = re.fullmatch(r'version\.[a-z0-9][a-z0-9_-]{0,63}\.(.+)', name)
    if match:
        child = match[1]
    field = _fields().get(child)
    hashed = (field is not None and (field.type == 'text' or child.endswith(('_path', '_audio')))
              or child.startswith(('text.', 'input_state.')) or child in {'source.identity', 'voices'}
              or bool(re.fullmatch(r'llm\.profile\.\d+\.provider', child)))
    if not hashed:
        return entry
    value = entry.get('value')
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        return None
    return {**entry, 'display': f'内容摘要 {value[:12]}'}


def _observable_issues(deps, key):
    changes = []
    for item in deps.unready(key):
        category = 'missing' if item['state'] in {'missing', 'deleted'} else 'parameter' if item['config_blocked'] else 'upstream'
        status = ('设置冲突' if item['config_blocked'] else '文件缺失' if item['state'] in {'missing', 'deleted'} else
                  '正在更新' if item['state'] == 'updating' else '上游需更新')
        changes.append(_change('required.' + item['step'] + '/' + item['name'], item['desc'] or item['name'], category,
            new=dict(display=status, origin=dict(kind='input', label='当前文件检查')),
            message=f"来自「{item['label']}」的 {item['name']}：{item['reason']}", upstream_step=item['step']))
    if key == 'tts':
        summary = deps.dubbing_summary()
        if summary['known'] and summary['pending']:
            ids = '、'.join(map(str, summary['pending_ids'][:20]))
            changes.append(_change('audio.pending', '选用的逐句配音', 'missing',
                new=dict(display=f"待补 {summary['pending']} 句", origin=dict(kind='input', label='当前音频检查')),
                message=f"仍有 {summary['pending']} 句缺少有效的选用音频（句号：{ids}{'等' if len(summary['pending_ids']) > 20 else ''}）；只需核对并补齐这些句子"))
        return changes
    own = [a for a in deps.arts.values() if a.step_key == key]
    if not own and deps.steps[key].status in {'done', 'stale'}:
        changes.append(_change('output.none', '本步生成文件', 'missing',
            new=dict(display='尚未登记生成文件', origin=dict(kind='input', label='当前文件检查')),
            message='没有已登记的正式产物，需要重新生成'))
    for art in own:
        path = deps.path(key, art.name)
        try:
            missing = not path or not path.is_file()
            changed = not missing and path.stat().st_size != art.size
        except OSError:
            missing, changed = True, False
        if missing or changed:
            changes.append(_change('output.' + art.name, art.name, 'missing' if missing else 'upstream',
                new=dict(display='文件缺失' if missing else '文件大小变化', origin=dict(kind='input', label='当前文件检查')),
                message='已登记的生成文件已丢失或无法读取' if missing else '生成文件大小与登记记录不同，需要重新生成或核对'))
    return changes


def explain(deps, key, stored=None):
    """详情按权威新鲜度显示；旧记录无证据时只给可观测原因，不倒推旧设置。"""
    if key not in STEP_MAP:
        raise DubForgeError('制作步骤不存在', code='STEP_UNKNOWN')
    step = deps.steps[key]
    previous, scoped = _history(deps, key, stored)
    warnings = []
    if previous is None:
        warnings.append('缺少可核对的上次生成输入快照，无法还原具体旧值；只显示当前能证实的原因。')
        try:
            raw = getattr(step, 'input_snapshot', '') if stored is None else stored
            raw = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(raw, dict) and raw.get('incomplete_comments') is True:
                warnings.append('上次记录缺少完整的实际烧录评论输入，不能据此还原整版制作差异。')
        except (ValueError, TypeError):
            pass
    if step.status in {'failed', 'interrupted'} and previous:
        warnings.append('此处比较上次成功生成的输入；最近一次制作未完成，现有文件仍需核验。')
    elif step.status in {'running', 'queued'} and previous:
        warnings.append('此处比较上次成功生成的输入；当前制作尚未完成，请在结束后核对结果。')
    try:
        fresh = bool(deps.display_fresh(key))
        changes = [] if fresh else _observable_issues(scoped, key)
        if previous and not fresh:
            current = capture(scoped, key, scope=previous['scope'],
                              fingerprint=previous['fingerprint'] if previous['scope'] == 'outputs' else None, _current=True)
            for name in sorted(set(previous['entries']) | set(current['entries'])):
                if not _valid_key(name):
                    continue
                old, new = _safe_previous(name, previous['entries'].get(name)), current['entries'].get(name)
                if old and new and old.get('value') == new.get('value'):
                    continue
                entry = new or old
                if not isinstance(entry, dict):
                    continue
                label = entry.get('label', name)
                message = f'「{label}」与上次成功生成时不同' if old else f'当前生成规则新增「{label}」；旧记录未保存此项'
                changes.append(_change(name, label, entry.get('category', 'parameter'), old=old, new=new, message=message))
        if not fresh and not changes:
            changes.append(_change('baseline.unknown', '输入或生成规则', 'unknown',
                message='输入指纹或步骤状态已变化；现有安全记录不足以确认具体改动，请核对当前参数与上游文件。'))
        feedback = deps.freshness_feedback(key)
        summary = ('当前输入与生成结果有效' if fresh else feedback.get('freshness_reason') or
                   ('尚未生成结果' if step.status == 'pending' else '正在制作，结束后再核对结果' if step.status in {'running', 'queued'} else
                    '；'.join(dict.fromkeys(c['message'] for c in changes[:3]))))
    except (DubForgeError, OSError, ValueError, TypeError, KeyError):
        fresh = False
        changes = [_change('baseline.unavailable', '生成输入', 'unknown', message='当前输入无法完整核对，请先检查设置与上游文件。')]
        summary = changes[0]['message']
        warnings.append('无法完整比较当前输入，未推断或补写历史参数。')
    return dict(step=key, fresh=fresh, history_available=previous is not None,
                summary=_safe_text(summary, 500), changes=changes, warnings=warnings)
