"""全流程的显式配方和两阶段预检；不下载、不调用生成模型、不投稿。"""
import copy
import hashlib
import json
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from dubforge import comment_studio, environment
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.assembly_outputs import VARIANT_LABELS, dimensions
from dubforge.pipeline.recovery import RecoveryDependencies as Dependencies
from dubforge.pipeline.registry import STEP_KEYS, STEP_MAP
from dubforge.publishing import service, transport

DEFAULT_RECIPE = dict(mode='draft', variants=['dub_hardsub_comments', 'src_hardsub_comments'],
    version_ids=None,
    part_titles={}, account_id='', template_id='', overrides={}, cover_source='platform', cover_id='',
    ai_copy=True, comments_mode='saved', comment_limit=50, comment_count=10, comment_failure_policy='skip',
    comment_prompt=comment_studio.FILTER_PROMPT, comment_settings=comment_studio.DEFAULT_SETTINGS)

# This is an optional, per-run scope.  It deliberately is not part of
# DEFAULT_RECIPE: old saved recipes and the settings page keep their original
# shape, while a caller can freeze the production steps selected for this run.
PRODUCTION_SCOPE_KEYS = tuple(key for key in STEP_KEYS if key != 'dubtask')
LABELS = {'src': '原片 · 原声不烧录', **VARIANT_LABELS}


def automatic_copy_fields(origins):
    """仅默认来源允许自动补齐，明确空值也属于用户已经设置的文案。"""
    from dubforge.publishing.copywriting import COPY_FIELDS
    return sorted(key for key in COPY_FIELDS if origins.get(key) == '默认')


class WorkflowError(DubForgeError):
    code = 'WORKFLOW_INVALID'
    suggestion = '请到对应步骤修正设置后重新检查；不会自动扩大范围或重新投稿。'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def recipe(raw):
    if not isinstance(raw, dict) or set(raw) - (set(DEFAULT_RECIPE) | {
            'production_steps', 'dubbing', 'chapters_enabled', 'chapters_translate', 'chapters_permanent',
            'chapters_instruction', 'update', 'confirm_new_publication', 'target_driven',
            'production_version_ids', 'delivery_mode', 'project_settings_fingerprint', 'project_goal_issues', 'from_step'}):
        raise WorkflowError('包含不支持的全流程设置')
    value = {**copy.deepcopy(DEFAULT_RECIPE), **copy.deepcopy(raw)}
    # 老流程不补开新副作用；新流程从项目发布设置显式冻结这三个开关。
    for key in ('chapters_enabled', 'chapters_translate', 'chapters_permanent'):
        if key in raw and type(raw[key]) is not bool:
            raise WorkflowError('章节开关必须明确选择')
    if 'chapters_instruction' in raw and (not isinstance(raw['chapters_instruction'], str)
            or len(raw['chapters_instruction']) > 2000):
        raise WorkflowError('章节标题自定义要求须为2000字以内的文本')
    if 'dubbing' in raw:
        from dubforge.pipeline.dubbing_policy import current
        value['dubbing'] = current(raw['dubbing'])
    if 'production_steps' in raw:
        selected = raw['production_steps']
        if (not isinstance(selected, list)
                or any(not isinstance(key, str) or key not in PRODUCTION_SCOPE_KEYS for key in selected)
                or len(selected) != len(set(selected))):
            raise WorkflowError('本次制作范围包含无效或重复步骤')
        value['production_steps'] = list(selected)
    if 'comment_settings' not in raw:
        value['comment_settings'] = comment_studio.default_settings()
    if not isinstance(value['mode'], str) or value['mode'] not in {'produce', 'draft', 'upload', 'publish', 'update'}:
        raise WorkflowError('请选择流程终点')
    if 'target_driven' in raw and type(raw['target_driven']) is not bool:
        raise WorkflowError('项目运行目标标记无效')
    if 'delivery_mode' in raw and (not isinstance(raw['delivery_mode'], str)
            or raw['delivery_mode'] not in {'produce', 'draft', 'upload', 'publish'}
            or value['mode'] != 'update' and raw['delivery_mode'] != value['mode']
            or value['mode'] == 'update' and raw['delivery_mode'] == 'produce'):
        raise WorkflowError('项目交付方式与执行授权不一致')
    if 'production_version_ids' in raw:
        ids = raw['production_version_ids']
        if (not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids)
                or len(set(ids)) != len(ids) or len(ids) > 32):
            raise WorkflowError('项目制作目标包含无效或重复版本')
    if raw.get('target_driven') and ('production_version_ids' not in raw
            or 'production_steps' in raw):
        raise WorkflowError('项目目标运行不能混入另一份技术步骤范围')
    if 'from_step' in raw and (not raw.get('target_driven') or not isinstance(raw['from_step'], str)
            or raw['from_step'] not in {*PRODUCTION_SCOPE_KEYS, 'comments'}):
        raise WorkflowError('请选择项目目标流程中的有效继续起点')
    if 'project_settings_fingerprint' in raw and (not isinstance(raw['project_settings_fingerprint'], str)
            or len(raw['project_settings_fingerprint']) != 64):
        raise WorkflowError('项目设置绑定无效，请重新检查')
    if 'project_goal_issues' in raw and (not isinstance(raw['project_goal_issues'], list)
            or any(not isinstance(item, dict) or set(item) != {'code', 'message', 'step'}
                   or any(not isinstance(item[key], str) for key in item)
                   for item in raw['project_goal_issues'])):
        raise WorkflowError('项目目标检查结果无效')
    if 'confirm_new_publication' in raw:
        if type(raw['confirm_new_publication']) is not bool:
            raise WorkflowError('请明确确认是否新建另一份稿件')
        if raw['confirm_new_publication'] and value['mode'] not in {'draft', 'upload', 'publish'}:
            raise WorkflowError('新建另一份稿件的确认仅用于新稿流程')
    if value['mode'] == 'update':
        target = raw.get('update')
        if (not isinstance(target, dict) or set(target) - {'publication_id', 'replacements', 'replace_videos', 'sync_fields'}
                or not isinstance(target.get('publication_id'), str) or not target['publication_id']
                or len(target['publication_id']) > 100):
            raise WorkflowError('请选择需要更新的原发布记录')
        if 'replace_videos' in target and type(target['replace_videos']) is not bool:
            raise WorkflowError('请明确选择是否替换视频文件')
        if 'sync_fields' in target:
            fields = target['sync_fields']
            if (not isinstance(fields, list) or len(fields) > 100
                    or any(not isinstance(key, str) or not key or len(key) > 100 for key in fields)
                    or len(fields) != len(set(fields))):
                raise WorkflowError('请选择不重复的稿件更新项目')
        replacements = target.get('replacements', [])
        if (not isinstance(replacements, list) or len(replacements) > 100
                or any(not isinstance(item, dict) or set(item) != {'old_cid', 'version_id'}
                    or type(item['old_cid']) is not int or item['old_cid'] <= 0
                    or not isinstance(item['version_id'], str) or not item['version_id']
                    or len(item['version_id']) > 100 for item in replacements)
                or len({item['old_cid'] for item in replacements}) != len(replacements)
                or len({item['version_id'] for item in replacements}) != len(replacements)):
            raise WorkflowError('每个原分P请选择一个不重复的成片版本')
    elif 'update' in raw:
        raise WorkflowError('更新原稿件范围只能用于更新流程')
    variants = value['variants']
    if not isinstance(variants, list) or not variants or any(not isinstance(v, str) or v not in LABELS for v in variants) or len(set(variants)) != len(variants):
        raise WorkflowError('请选择至少一种成品方案，且不能重复')
    video_update = value['mode'] == 'update' and (value['update'].get('replace_videos', True)
                                                or 'add_parts' in value['update'].get('sync_fields', []))
    if value['version_ids'] is not None and (not isinstance(value['version_ids'], list)
            or not value['version_ids'] and not value.get('target_driven') and (value['mode'] != 'update' or video_update)
            or any(not isinstance(v, str) for v in value['version_ids']) or len(set(value['version_ids'])) != len(value['version_ids'])):
        raise WorkflowError('请选择至少一个不重复的成片版本')
    if not isinstance(value['part_titles'], dict) or set(value['part_titles']) - (set(value['version_ids']) if value['version_ids'] is not None else set(LABELS)):
        raise WorkflowError('分P名称格式无效')
    for title in value['part_titles'].values():
        if not isinstance(title, str) or not title.strip() or len(title.encode('utf-16-le')) // 2 > 80:
            raise WorkflowError('分P名称须为1–80个字符')
    if type(value['ai_copy']) is not bool:
        raise WorkflowError('流程开关必须明确选择')
    for field in ('account_id', 'template_id', 'cover_id'):
        if not isinstance(value[field], str) or len(value[field]) > 200:
            raise WorkflowError('账号、模板或封面标识无效')
    if value['cover_source'] not in ('platform', 'custom') or value['comments_mode'] not in ('saved', 'auto'):
        raise WorkflowError('请选择有效的封面和评论来源')
    if value['comment_failure_policy'] not in ('skip', 'stop'):
        raise WorkflowError('请选择有效的评论失败处理方式')
    if any(type(value[k]) is not int or not 1 <= value[k] <= limit for k, limit in [('comment_limit', 500), ('comment_count', 100)]):
        raise WorkflowError('采集评论须为1–500条，上屏评论须为1–100条')
    if value['comment_count'] > value['comment_limit']:
        raise WorkflowError('上屏数量不能超过采集上限')
    if not isinstance(value['comment_prompt'], str) or not value['comment_prompt'].strip() or len(value['comment_prompt']) > 2000:
        raise WorkflowError('请填写2000字以内的评论筛选要求')
    value['comment_settings'] = comment_studio.validate_settings(value['comment_settings'])
    value['overrides'] = service.validate_values(value['overrides'])
    return value


def configuration_snapshot(deps, *, version_mode=False, steps=None, legacy=False):
    # 只落不可逆指纹，不把 LLM 密钥、代理凭据写进流程数据库或 API 响应。
    # steps 限定参与指纹的步骤集合: 全流程按「剩余步骤实际消费的键」对比,
    # 与已完成步骤相关的修改不打断流程 (ADR-0075)。
    from dubforge.pipeline.config_fingerprint import snapshot
    names = list(STEP_MAP) if steps is None else [name for name in steps if name in STEP_MAP]
    if version_mode:
        names = [name for name in names if name not in {'assemble', 'subtitles'}]
    # 每个步骤按自己实际选择的模型取快照，固定模型组不跟随无关组漂移。
    values = {name: snapshot(deps, name, execution=True) for name in names}
    if not legacy and (steps is None or 'comments' in steps):
        values['comments'] = snapshot(deps, 'comments', keys=('llm', 'content_review'), execution=True)
    if legacy:
        keys = {key for name in names for key in STEP_MAP[name].cfg_keys}
        values = {key: deps.cfg(key) for key in sorted(keys)}
    from dubforge.pipeline.video_source import resolve_source
    local = resolve_source(deps.project, deps.root) if deps.project.source_kind == 'local' else None
    stat = local.stat() if local and local.is_file() else None
    return dict(source=[deps.project.source_kind, deps.project.source_url, [stat.st_size, stat.st_mtime_ns] if stat else None],
                settings=values, **({} if legacy else {'schema': 2}))


def configuration_stamp(deps, *, version_mode=False, steps=None, legacy=False):
    return digest(configuration_snapshot(deps, version_mode=version_mode, steps=steps, legacy=legacy))


def dubbing_dependencies(deps, planned, summary, include_dependencies):
    """主动配音消费当前声音参考；旧音频可用不代表新参考已准备好。"""
    requirements = (summary or {}).get('requirements') or []
    if not requirements:
        return planned
    original_steps = set(planned['steps']) - set(planned.get('added') or [])
    producers = {item['step'] for item in requirements
                 if item.get('step') in STEP_MAP and not item.get('config_blocked')}
    targets = set(planned['steps']) | (producers if include_dependencies else set())
    result = deps.plan([key for key in STEP_KEYS if key in targets], include_dependencies)
    merged = {(item['step'], item['name']): item for item in result['requirements']}
    for item in requirements:
        merged[(item['step'], item['name'])] = item
        if item.get('config_blocked') or item['step'] not in result['steps']:
            if not any(old['step'] == item['step'] and old['name'] == item['name'] for old in result['blockers']):
                result['blockers'].append({**item, 'consumer': 'tts'})
    result['requirements'] = list(merged.values())
    result['added'] = [key for key in result['steps'] if key not in original_steps]
    result['missing'] = [item for item in result['blockers'] if item['state'] in {'missing', 'deleted'}]
    result['stale'] = [item for item in result['blockers'] if item['state'] == 'stale']
    return result


def target_closure(deps, targets):
    """按实际消费关系求完整闭包，包含已有效输入的生产者。"""
    required = set()
    def visit(key):
        if key in required:
            return
        required.add(key)
        for producer, _name, _label in deps.inputs(key):
            if producer in STEP_MAP:
                visit(producer)
    for key in targets:
        visit(key)
    return required


def pending_production(deps, steps):
    """生产者设置变化也须检查，并向实际消费者传播待更新说明。"""
    from dubforge.pipeline.recovery import cache_usable
    pending = {key for key in steps if key in STEP_MAP and not cache_usable(deps, key)}
    changed = True
    while changed:
        changed = False
        for key in steps:
            if key in STEP_MAP and key not in pending and any(producer in pending
                    for producer, _name, _label in deps.inputs(key)):
                pending.add(key)
                changed = True
    return [key for key in steps if key in pending]


def production_versions(deps, versions, pending=()):
    """只说明本次选中版本，复用同一依赖/摘要缓存，不扫描无关历史成片。"""
    from dubforge.pipeline import assembly_versions, version_render
    rows = []
    for version in versions or []:
        artifact = assembly_versions.result_artifact(deps, version)
        direct = False
        try:
            ctx = version_render.VersionContext(deps, version)
            direct = version_render.uses_source_directly(ctx, version)
            if '_content_redaction_plan' in ctx.__dict__:
                deps._content_redaction_plan = ctx._content_redaction_plan
            fresh = (not deps.for_version(version).unready('assemble') if direct
                     else assembly_versions.is_current(deps, version))
            reason = ('原片可直接复用' if direct and fresh else
                      '源视频就绪后直接使用，无需编码' if direct else
                      '有效成片可复用' if fresh else
                      '当前设置或输入已有变化，需更新成片' if artifact else '尚未生成，需要制作')
            changed_inputs = (set(pending) - {'assemble'}) & target_closure(deps.for_version(version), ['assemble'])
            if changed_inputs:
                fresh = False
                reason = '先准备或更新所需上游结果，再按新输入核对成片' if not direct else '先准备源视频，再直接使用，无需编码'
        except (DubForgeError, OSError) as exc:
            fresh = False
            reason = exc.message if isinstance(exc, DubForgeError) else '成片文件暂不可读取，需重新检查'
        rows.append(dict(id=version['id'], name=version['name'], fresh=fresh, reason=reason,
                         uses_source_directly=direct))
    return rows


def production_checks(deps, steps, *, online=False, needs_ai=False, dubbing_summary=None):
    issues = []
    def check(label, fn):
        try:
            fn()
        except DubForgeError as exc:
            issues.append(f'{label}：{exc.message}')
    for tool in (('ffmpeg', 'ffprobe') if steps else ()):
        if not shutil.which(tool):
            issues.append(f'缺少 {tool}，请完成环境安装')
    if 'preprocess' in steps and deps.cfg('separate.enabled', True):
        check('音轨分离', lambda: environment.require_component('separation'))
    if 'transcribe' in steps:
        if deps.cfg('asr.input_mode', 'speech') == 'speech':
            check('语音识别', lambda: environment.require_component('asr'))
        else:
            from dubforge.publishing.imports import selected
            check('平台字幕', lambda: selected(deps.root))
    if needs_ai or 'translate' in steps or 'split' in steps and deps.cfg('split.use_llm', True):
        from dubforge.backends.llm import _profiles
        selections = {''} if needs_ai else set()
        if 'translate' in steps:
            selections.add(deps.cfg('translate.llm_profile', ''))
        if 'split' in steps and deps.cfg('split.use_llm', True):
            selections.add(deps.cfg('split.llm_profile', ''))
        for selected in selections:
            try:
                profiles = _profiles(selected)
                if not profiles or any(not p.get('base_url') or not p.get('model') for p in profiles):
                    issues.append('LLM：请配置并启用有效的接口和模型')
            except DubForgeError as exc:
                issues.append(f'LLM：{exc.message}')
    if 'tts' in steps and (dubbing_summary is None or dubbing_summary.get('deferred')
                          or dubbing_summary.get('generate', 0) > 0 or dubbing_summary.get('repair', 0) > 0):
        from dubforge import tts_requests, tts_service
        backend = deps.cfg('tts.backend')
        if backend not in tts_service._BACKENDS:
            issues.append('配音引擎无效')
        elif backend == 'indextts25' and tts_requests.needs_recovery(deps.cfg(f'tts.{backend}.url')):
            # 未知的远端请求不是一次可立即重跑的失败；先给出恢复入口，避免
            # 用户反复确认后才各创建一个注定被防重保护拦截的配音任务。
            issues.append(f'配音服务需要恢复：{tts_requests.RECOVERY_HINT}')
        elif online and not tts_service._BACKENDS[backend].is_online(deps.cfg(f'tts.{backend}.url')):
            issues.append('配音服务未就绪，请先在配音页启动服务')
        # 已知范围由中央配音计划按实际逐句参数检查参考文件；全局缺文件
        # 不应误拦全部使用独立参考的句子。尚无原文时仍检查项目基础参考。
        if backend == 'indextts25' and (dubbing_summary is None or dubbing_summary.get('deferred')):
            for part, label in [('timbre', '音色'), ('emo', '情感')]:
                if deps.cfg(f'tts.indextts25.{part}_mode') == 'preset' and not Path(deps.cfg(f'tts.indextts25.{part}_audio') or '').is_file():
                    issues.append(f'{label}参考音频不存在，请选择预设或原声参考模式')
    if ('assemble' in steps and dubbing_summary is not None
            and not dubbing_summary.get('deferred') and not dubbing_summary.get('generate')
            and not dubbing_summary.get('repair')):
        # 可修复的冲突进入配音执行；确实没有可执行修复时才在开始前说明。
        from dubforge.pipeline import voice_timing
        rows = [{**row, 'source': row['source'] if row['source'] == 'original'
                 else 'dub' if row.get('take') is not None else 'silent'}
                for row in deps.production_lines()]
        layout = voice_timing.plan(deps.project, deps.root, rows, deps.cfg, dependencies=deps)
        if (layout['timing']['enabled'] or layout['timing'].get('manual')) and layout['timing']['unresolved_ids']:
            ids = layout['timing']['unresolved_ids']
            reasons = list(dict.fromkeys(row['reason'] for row in dubbing_summary.get('repair_blocked', [])))
            issues.append('配音排布：第 ' + '、'.join(map(str, ids[:12]))
                          + ' 句当前没有可执行的自动修复。'
                          + ('；'.join(reasons[:3]) + '。' if reasons else '')
                          + '请开启自动修复，或调整人工位置、滑动范围、缩短因子后重新检查。')
    return list(dict.fromkeys(issues))


def plan(uid, raw, *, online=True, dubbing_operation_id=None, previous_update=None):
    spec = recipe(raw)
    project = crud.get_project(uid)
    if not project:
        raise WorkflowError('项目不存在', code='PROJECT_NOT_FOUND')
    updating = spec['mode'] == 'update'
    needs_video_outputs = updating and (spec['update'].get('replace_videos', True)
                                       or 'add_parts' in spec['update'].get('sync_fields', []))
    target_driven = spec.get('target_driven', False)
    if updating and 'production_steps' not in spec and not target_driven:
        from dubforge.pipeline.dependencies import selected_steps
        # 更新目标不决定制作起点；视频更新沿用项目勾选，纯资料更新默认不制作。
        # 冻结推导后的范围，使确认与恢复不受后续勾选变化影响。
        spec['production_steps'] = selected_steps(project) if needs_video_outputs else []
    requested_scope = spec.get('production_steps')
    variants = [v for v in spec['variants'] if v != 'src']
    deps = Dependencies(project, assemble_variants=variants or None)
    version_specs = None
    production_ids = spec.get('production_version_ids') if target_driven else spec['version_ids']
    if target_driven and not production_ids:
        variants = []
    if production_ids and (not updating or needs_video_outputs
            or 'assemble' in (requested_scope or [])):
        from dubforge.pipeline import assembly_versions
        definitions = assembly_versions.selected(uid, production_ids)
        overlay = comment_studio.effective_overlay(project.uid, comment_studio._read(project).get('overlay'))
        version_specs = [assembly_versions.resolve(deps, v, comment_plan=overlay,
            comment_settings=spec['comment_settings'] if spec['comments_mode'] == 'auto' else None) for v in definitions]
        deps = deps.with_versions(version_specs)
        variants = [v for v in version_specs if v.get('output_cuts') or v.get('timeline') or v.get('update')
                    or any(v[k] for k in ('dubbing', 'burn_subtitles', 'include_comments'))]
    if target_driven:
        # 正常运行仅以目标版本反推实际依赖；不读取旧auto_steps，也不强插下载。
        targets = ['assemble'] if production_ids else []
        required = target_closure(deps, targets)
        start_issues = []
        if spec.get('from_step'):
            # 从实际消费关系求完整闭包，包含已经有效的上游；不能因缓存有效
            # 就误判它不属于流程，也不能把无关的历史步骤强塞进项目目标。
            if any(v.get('include_comments') and not v.get('update') and not v.get('timeline')
                   for v in version_specs or []):
                required.add('comments')
            if spec['from_step'] not in required:
                start_issues.append(dict(code='PROJECT_EXECUTION_START_UNUSED', step=spec['from_step'],
                    message='当前项目目标不需要此步骤，请修改目标成片，或使用「仅运行本步／运行到此步」。'))
            elif spec['from_step'] in STEP_MAP and spec['from_step'] not in targets:
                targets.append(spec['from_step'])
        # 即使消费者当前文件有效，也核对生产者自己的输入指纹；否则只改
        # 翻译风格或识别模型时会漏更新。有效节点由Runner各自复用，不强制重做。
        full_targets = [key for key in STEP_KEYS if key in required]
        planned = (deps.plan(full_targets, True) if full_targets else
                   dict(steps=[], added=[], requirements=[], blockers=[], missing=[], stale=[]))
        steps = list(planned['steps'])
        requested_steps = list(targets)
        added_steps = [key for key in steps if key not in requested_steps]
    elif requested_scope is None:
        # Legacy/full-flow recipes retain their historical behaviour: start at
        # download and finish at the selected assembly output.
        targets = ['assemble'] if variants else ['download']
        planned = deps.plan(targets, True)
        steps = ['download', *[s for s in planned['steps'] if s != 'download']]
        requested_steps = list(targets)
        added_steps = [s for s in steps if s not in requested_steps]
    else:
        requested_steps = list(requested_scope)
        # A publish/draft run always needs the actual assembly endpoint, while
        # a production-only run executes exactly the checked scope plus its
        # missing/stale dependencies.  In particular, do not force download
        # or assemble for an explicit production run.
        targets = list(requested_steps)
        if spec['mode'] in {'draft', 'upload', 'publish'} or needs_video_outputs:
            targets.append('assemble')
        if not targets:
            planned = {'steps': [], 'added': [], 'requirements': [], 'blockers': [],
                       'missing': [], 'stale': []}
            steps = []
            added_steps = []
        else:
            planned = deps.plan(targets, True)
            steps = list(planned['steps'])
            added_steps = [s for s in steps if s not in requested_steps]
        # Produce with an empty scope has no meaningful action.  Keep this as
        # a preflight blocker instead of silently turning it into a full run.
        if spec['mode'] == 'produce' and not requested_steps:
            planned['blockers'].append({'label': '本次运行范围', 'reason': '至少勾选一个制作步骤',
                                        'step': '', 'name': 'production_steps', 'state': 'missing'})
    assembly_requested = (bool(production_ids) if target_driven else
                          ('assemble' in steps or requested_scope is None
                           or spec['mode'] in {'draft', 'upload', 'publish'} or needs_video_outputs))
    comments = assembly_requested and any(v['include_comments'] and not v.get('update') and not v.get('timeline') if isinstance(v, dict) else dimensions(v)['include_comments'] for v in variants)
    # A production-only TTS selection still needs the prepared sentence/audio
    # contract; unrelated selections (for example preprocess alone) do not.
    from dubforge.pipeline.assembly_versions import requires_dubbing
    needs_dubbing = 'tts' in steps or (assembly_requested and
        any(requires_dubbing(v) if isinstance(v, dict) else dimensions(v)['dubbing'] for v in variants))
    # 配音有效性与本次生成意图分开：旧音频可使用，用户仍可明确要求更新。
    dubbing_run = None
    if needs_dubbing:
        from dubforge.pipeline import dubbing_policy
        dubbing_run = dubbing_policy.preview(project, spec.get('dubbing'), dependencies=deps,
                                             operation_id=dubbing_operation_id)
        if (spec.get('dubbing', {}).get('mode', 'fill') != 'fill' or dubbing_run.get('repair')) and 'tts' not in steps:
            steps = [key for key in STEP_KEYS if key in set(steps) | {'tts'}]
            if 'tts' not in requested_steps and 'tts' not in added_steps:
                added_steps.append('tts')
        if dubbing_run.get('requirements'):
            planned = dubbing_dependencies(deps, {**planned, 'steps': steps, 'added': added_steps}, dubbing_run, True)
            steps = planned['steps']
            added_steps = [key for key in steps if key not in requested_steps]
    alignment_error = None
    # 配音的实际输入不是“两个文件存在”这么简单；split 与 translate
    # 必须逐句同构。依赖规划在缺文件时仍需返回 missing blocker，只有
    # 文件存在但合同被破坏时才把底层 TEXT_ALIGNMENT 记录为预检 blocker，
    # 避免启动后才在 tts 执行阶段失败；预检仍返回完整计划，供网页显示
    # 错误码、建议和恢复起点。
    if needs_dubbing:
        try:
            deps.dubbing_lines(required=True)
        except DubForgeError as exc:
            if exc.code in {'TEXT_ALIGNMENT', 'TEXT_EMPTY', 'TEXT_TIMELINE'}:
                alignment_error = exc
    issues = [f"{i['label']}：{i['reason']}" for i in planned['blockers']]
    issues.extend(item['message'] for item in spec.get('project_goal_issues', []))
    if target_driven:
        issues.extend(item['message'] for item in start_issues)
    warnings = (['同版本成品成功后替换旧文件；未变化的版本复用，不会增加重复成品。' if version_specs else '同组合成品成功后替换旧文件；不会增加重复成品。']
                if assembly_requested else [])
    if spec.get('confirm_new_publication'):
        warnings.append('本次按新稿流程准备，投稿后会获得新的BV号；原稿件及其视频、评论和数据保留，不会被覆盖。')
    if dubbing_run and dubbing_run.get('repair'):
        warnings.append(f"配音排布将自动修复 {dubbing_run['repair']} 句：先滑动，再按已配置因子有限缩短；沿用声音生成参数，并保留当前槽的缩时前音频。")
    if alignment_error:
        # 句数/编号不一致属于可恢复的上游合同失效：恢复计划会把 split、translate
        # 一起加入执行队列，重新生成同构输入后再进入 TTS。不能在这里当成硬 blocker，
        # 否则完整批次永远无法启动，只会在预检阶段重复报告同一个旧结果。
        warnings.append(f"字幕与配音输入需更新：{alignment_error.message}；将先重建语义整理与翻译结果。")
    overlay_revision = None
    if steps and not deps.root.is_dir():
        issues.append('项目目录不存在')
    from dubforge.pipeline.video_source import resolve_source
    if steps and project.source_kind == 'local' and not resolve_source(project, deps.root):
        issues.append('本地原视频不存在')
    elif steps and project.source_kind != 'local':
        try:
            source = urlsplit(project.source_url)
            if source.scheme not in {'http', 'https'} or not source.hostname:
                raise ValueError()
        except ValueError:
            issues.append('请填写有效的视频链接')
    if comments:
        if spec['comments_mode'] == 'saved':
            state = comment_studio.view(uid)
            overlay_revision = state['overlay_revision']
            try:
                comment_studio.confirmed(uid, overlay_revision)
            except DubForgeError as exc:
                issues.append(exc.message)
        else:
            from dubforge.video_info import _youtube
            if not _youtube(project.source_url):
                issues.append('自动采集评论目前仅支持 YouTube 视频')
            warnings.append(f"自动采集最多{spec['comment_limit']}条评论，评分后最多上屏{spec['comment_count']}条；零条可用会暂停。")
    settings, origins, account_name, cover_hash = {}, {}, '', ''
    publish_metadata = None
    copy_fields = []
    update_plan = None
    if spec['mode'] == 'update':
        from dubforge import workflow_update
        update_plan = workflow_update.prepare(uid, spec, version_specs, online=online, previous=previous_update)
        issues.extend(update_plan.get('blockers', []))
        warnings.extend(update_plan.get('warnings', []))
        account_name = update_plan.get('account_name', '')
    elif spec['mode'] != 'produce':
        try:
            settings, origins = service.resolve_defaults(spec['account_id'], spec['template_id'], spec['overrides'])
            copy_fields = automatic_copy_fields(origins)
            account_name = next(a['name'] for a in service.catalog()['accounts'] if a['id'] == spec['account_id'])
            if not settings['human_type2']: issues.append('发布：请选择分区')
            if not settings['tags'] and (not spec['ai_copy'] or origins['tags'] != '默认'):
                issues.append('发布：请填写标签，或恢复继承并启用自动文案')
            if not settings['title'] and origins['title'] != '默认':
                issues.append('发布：手动指定的标题为空，请填写或恢复继承以自动填充')
            if settings['declaration_id'] is None: issues.append('发布：请选择创作声明')
            if settings['season_id'] and not settings['section_id']: issues.append('发布：请选择合集小节')
            probe = {**settings, 'title': settings['title'] or '待按视频资料填充',
                     'tags': settings['tags'] or ['预检占位'], 'source': settings['source'] or (project.source_url if project.source_kind != 'local' else '')}
            service._validate_local_settings(probe)
            if not transport.available(): issues.append('发布组件尚未安装')
            elif online:
                publish_metadata = service.metadata(spec['account_id'])
                service._validate_submission({'settings': probe}, publish_metadata)
            if spec['cover_source'] == 'custom':
                cover_hash = service._file_hash(service._cover_path(spec['cover_id']))
            else:
                warnings.append('下载后使用平台封面；来源无封面时暂停，请补选自定义封面。')
        except DubForgeError as exc:
            issues.append(f'发布：{exc.message}')
        if spec['ai_copy'] and copy_fields:
            warnings.append('自动流程仅用 AI 补充未单独设置的文案；项目或模板中已明确设置的值（包括空值）保持不变，生成失败时暂停。')
        if spec.get('chapters_enabled'):
            warnings.append('审核通过后自动同步各分P的视频章节；无源章节则跳过。章节异常单独提示，不会重新上传或投稿。')
        if any(v['dubbing'] if isinstance(v, dict) else dimensions(v)['dubbing'] for v in variants):
            warnings.append('含配音版本：请核对所选创作声明是否适用；软件不强制改选AI声明。')
    pending_steps = pending_production(deps, steps) if target_driven else steps
    check_dubbing = dubbing_run
    if (target_driven and dubbing_run and 'tts' in pending_steps
            and {'split', 'translate'} & set(pending_steps)):
        check_dubbing = {**dubbing_run, 'deferred': True}
    issues += production_checks(deps, pending_steps, online=online,
        needs_ai=bool(spec['mode'] not in {'produce', 'update'} and spec['ai_copy'] and copy_fields)
        or comments and spec['comments_mode'] == 'auto', dubbing_summary=check_dubbing)
    stamp = configuration_stamp(deps, version_mode=version_specs is not None,
                                steps=steps if target_driven else None)
    snapshot = configuration_snapshot(deps, version_mode=version_specs is not None,
                                      steps=steps if target_driven else None)
    binding = dict(recipe=spec, configuration=stamp, settings=settings, overlay_revision=overlay_revision, cover_hash=cover_hash)
    if version_specs is not None: binding['version_specs'] = version_specs
    if update_plan is not None:
        # 业务范围参与确认；展示名称、获取时间和服务警告不改变更新授权。
        binding['update_plan'] = {key: value for key, value in update_plan.items()
                                  if key not in {'settings_display', 'scope_options', 'warnings', 'blockers'}}
    dubbing = {**(deps.dubbing_summary() if needs_dubbing else {}), 'needed': needs_dubbing}
    if check_dubbing is not None:
        dubbing.update(check_dubbing)
    if alignment_error:
        dubbing.update(error=alignment_error.message, error_code=alignment_error.code,
                       suggestion=alignment_error.suggestion, detail=alignment_error.detail)
    return dict(recipe=spec, steps=steps, requested_steps=requested_steps, added_steps=added_steps,
                labels={s: STEP_MAP[s].label for s in steps},
                dubbing=dubbing,
                blockers=list(dict.fromkeys(issues)), warnings=warnings, settings=settings, origins=origins,
                settings_display=(update_plan.get('settings_display', {}) if update_plan is not None else service.settings_display(spec['account_id'], settings,
                    metadata=publish_metadata, account_name=account_name) if spec['mode'] != 'produce' else {}),
                account_name=account_name, configuration=stamp, configuration_snapshot=snapshot,
                fingerprint=digest(binding), overlay_revision=overlay_revision, cover_hash=cover_hash,
                **({'goal_issues': [*copy.deepcopy(spec.get('project_goal_issues', [])), *start_issues],
                    'requested_start_step': spec.get('from_step', ''),
                    'production_versions': production_versions(deps, version_specs, pending_steps),
                    'pending_production_steps': pending_steps,
                    'production_version_ids': list(production_ids or []),
                    'publication_version_ids': list(spec['version_ids'] or [])} if target_driven else {}),
                **({'update_plan': update_plan} if update_plan is not None else {}),
                **({'version_specs': version_specs} if version_specs is not None else {}))
