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
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.registry import STEP_MAP
from dubforge.publishing import service, transport

DEFAULT_RECIPE = dict(mode='draft', variants=['dub_hardsub_comments', 'src_hardsub_comments'],
    version_ids=None,
    part_titles={}, account_id='', template_id='', overrides={}, cover_source='platform', cover_id='',
    ai_copy=True, comments_mode='saved', comment_limit=50, comment_count=10, comment_failure_policy='skip',
    comment_prompt=comment_studio.FILTER_PROMPT, comment_settings=comment_studio.DEFAULT_SETTINGS)
LABELS = {'src': '原片 · 原声不烧录', **VARIANT_LABELS}


class WorkflowError(DubForgeError):
    code = 'WORKFLOW_INVALID'
    suggestion = '请到对应步骤修正设置后重新检查；不会自动扩大范围或重新投稿。'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def recipe(raw):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULT_RECIPE):
        raise WorkflowError('包含不支持的全流程设置')
    value = {**copy.deepcopy(DEFAULT_RECIPE), **copy.deepcopy(raw)}
    if 'comment_settings' not in raw:
        value['comment_settings'] = comment_studio.default_settings()
    if not isinstance(value['mode'], str) or value['mode'] not in {'produce', 'draft', 'publish'}:
        raise WorkflowError('请选择流程终点')
    variants = value['variants']
    if not isinstance(variants, list) or not variants or any(not isinstance(v, str) or v not in LABELS for v in variants) or len(set(variants)) != len(variants):
        raise WorkflowError('请选择至少一种成品方案，且不能重复')
    if value['version_ids'] is not None and (not isinstance(value['version_ids'], list) or not value['version_ids']
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


def configuration_snapshot(deps, *, version_mode=False, steps=None):
    # 只落不可逆指纹，不把 LLM 密钥、代理凭据写进流程数据库或 API 响应。
    # steps 限定参与指纹的步骤集合: 全流程按「剩余步骤实际消费的键」对比,
    # 与已完成步骤相关的修改不打断流程 (ADR-0075)。
    if steps is None:
        keys = {key for step in STEP_MAP.values() for key in step.cfg_keys}
    else:
        keys = {key for name in steps if name in STEP_MAP for key in STEP_MAP[name].cfg_keys}
    if version_mode:
        keys -= {'assemble', 'subtitles'}  # 版本呈现参数已单独冻结；不随全局样式漂移。
    from dubforge.pipeline.video_source import resolve_source
    local = resolve_source(deps.project, deps.root) if deps.project.source_kind == 'local' else None
    stat = local.stat() if local and local.is_file() else None
    return dict(source=[deps.project.source_kind, deps.project.source_url, (stat.st_size, stat.st_mtime_ns) if stat else None],
                settings={key: deps.cfg(key) for key in sorted(keys)})


def configuration_stamp(deps, *, version_mode=False, steps=None):
    return digest(configuration_snapshot(deps, version_mode=version_mode, steps=steps))


def production_checks(deps, steps, *, online=False, needs_ai=False):
    issues = []
    def check(label, fn):
        try:
            fn()
        except DubForgeError as exc:
            issues.append(f'{label}：{exc.message}')
    for tool in ('ffmpeg', 'ffprobe'):
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
    if 'tts' in steps:
        from dubforge import tts_service
        backend = deps.cfg('tts.backend')
        if backend not in tts_service._BACKENDS:
            issues.append('配音引擎无效')
        elif online and not tts_service._BACKENDS[backend].is_online(deps.cfg(f'tts.{backend}.url')):
            issues.append('配音服务未就绪，请先在配音页启动服务')
        if backend == 'indextts25':
            for part, label in [('timbre', '音色'), ('emo', '情感')]:
                if deps.cfg(f'tts.indextts25.{part}_mode') == 'preset' and not Path(deps.cfg(f'tts.indextts25.{part}_audio') or '').is_file():
                    issues.append(f'{label}参考音频不存在，请选择预设或原声参考模式')
    return list(dict.fromkeys(issues))


def plan(uid, raw, *, online=True):
    spec = recipe(raw)
    project = crud.get_project(uid)
    if not project:
        raise WorkflowError('项目不存在', code='PROJECT_NOT_FOUND')
    variants = [v for v in spec['variants'] if v != 'src']
    deps = Dependencies(project, assemble_variants=variants or None)
    version_specs = None
    if spec['version_ids'] is not None:
        from dubforge.pipeline import assembly_versions
        definitions = assembly_versions.selected(uid, spec['version_ids'])
        overlay = comment_studio.effective_overlay(project.uid, comment_studio._read(project).get('overlay'))
        version_specs = [assembly_versions.resolve(deps, v, comment_plan=overlay,
            comment_settings=spec['comment_settings'] if spec['comments_mode'] == 'auto' else None) for v in definitions]
        deps = deps.with_versions(version_specs)
        variants = [v for v in version_specs if any(v[k] for k in ('dubbing', 'burn_subtitles', 'include_comments'))]
    planned = deps.plan(['assemble'] if variants else ['download'], True)
    # 全流程从视频获取开始；其他依赖仍按实际产物决定，不强行运行无用的配音。
    steps = ['download', *[s for s in planned['steps'] if s != 'download']]
    comments = any(v['include_comments'] if isinstance(v, dict) else dimensions(v)['include_comments'] for v in variants)
    needs_dubbing = any(v['dubbing'] if isinstance(v, dict) else dimensions(v)['dubbing'] for v in variants)
    issues = [f"{i['label']}：{i['reason']}" for i in planned['blockers']]
    warnings = ['同版本成品成功后替换旧文件；未变化的版本复用，不会增加重复成品。' if version_specs else '同组合成品成功后替换旧文件；不会增加重复成品。']
    overlay_revision = None
    if not deps.root.is_dir():
        issues.append('项目目录不存在')
    from dubforge.pipeline.video_source import resolve_source
    if project.source_kind == 'local' and not resolve_source(project, deps.root):
        issues.append('本地原视频不存在')
    elif project.source_kind != 'local':
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
    issues += production_checks(deps, steps, online=online,
        needs_ai=(spec['mode'] != 'produce' and spec['ai_copy']) or comments and spec['comments_mode'] == 'auto')
    settings, origins, account_name, cover_hash = {}, {}, '', ''
    if spec['mode'] != 'produce':
        try:
            settings, origins = service.resolve_defaults(spec['account_id'], spec['template_id'], spec['overrides'])
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
            elif online: service._validate_submission({'settings': probe}, service.metadata(spec['account_id']))
            if spec['cover_source'] == 'custom':
                cover_hash = service._file_hash(service._cover_path(spec['cover_id']))
            else:
                warnings.append('下载后使用平台封面；来源无封面时暂停，请补选自定义封面。')
        except DubForgeError as exc:
            issues.append(f'发布：{exc.message}')
        if spec['ai_copy']:
            warnings.append('AI 自动填充未手动指定的标题、简介、标签和动态；失败时暂停，不用空文案投稿。')
        if any(v['dubbing'] if isinstance(v, dict) else dimensions(v)['dubbing'] for v in variants):
            warnings.append('含配音版本：请核对所选创作声明是否适用；软件不强制改选AI声明。')
    stamp = configuration_stamp(deps, version_mode=version_specs is not None)
    snapshot = configuration_snapshot(deps, version_mode=version_specs is not None)
    binding = dict(recipe=spec, configuration=stamp, settings=settings, overlay_revision=overlay_revision, cover_hash=cover_hash)
    if version_specs is not None: binding['version_specs'] = version_specs
    return dict(recipe=spec, steps=steps, labels={s: STEP_MAP[s].label for s in steps},
                dubbing={**(deps.dubbing_summary() if needs_dubbing else {}), 'needed': needs_dubbing},
                blockers=list(dict.fromkeys(issues)), warnings=warnings, settings=settings, origins=origins,
                account_name=account_name, configuration=stamp, configuration_snapshot=snapshot,
                fingerprint=digest(binding), overlay_revision=overlay_revision, cover_hash=cover_hash,
                **({'version_specs': version_specs} if version_specs is not None else {}))
