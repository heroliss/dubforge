"""配置唯一入口：范围、稀疏覆盖、来源、校验和绑定在同一个边界内。"""
import copy
import hashlib
import json
import logging
import threading
import uuid

from sqlmodel import Session, select

from dubforge import config
from dubforge.configuration_models import ConfigurationScope
from dubforge.db import get_engine
from dubforge.errors import DubForgeError

gate = threading.RLock()
logger = logging.getLogger(__name__)

# 发布平台字段由独立的发布设置表单编辑，不会进入通用 configuration schema；
# 但它们仍会出现在“应用最新设置”的差异预览中，因此这里保留同一套面向用户的名称。
_BINDING_LABELS = {
    'run.mode': '执行终点',
    'assembly.versions': '成片版本',
    'assembly.target_version_ids': '项目目标成片',
    'publish.project_target': '项目发布目标',
    'publish.account_id': 'B站账号',
    'publish.title': '标题',
    'publish.description': '简介',
    'publish.tags': '标签',
    'publish.tid': '分区（兼容字段）',
    'publish.human_type2': '分区',
    'publish.copyright': '稿件类型',
    'publish.source': '原视频来源',
    'publish.declaration_id': '创作声明',
    'publish.visibility': '可见范围',
    'publish.watermark': '水印',
    'publish.close_comments': '关闭评论',
    'publish.close_danmaku': '关闭弹幕',
    'publish.selected_comments': '精选评论',
    'publish.dynamic': '粉丝动态',
    'publish.publish_at': '定时发布',
    'publish.season_id': '合集',
    'publish.section_id': '合集小节',
    'publish.topic_id': '活动/话题',
    'publish.topic_name': '活动/话题名称',
    'publish.mission_id': '活动任务',
    'publish.ai_copy': '自动补全文案',
    'publish.cover_source': '封面来源',
    'publish.cover_id': '指定封面',
    'publish.version_ids': '发布分P',
    'publish.part_titles': '分P名称',
    'publish.chapters_enabled': '投稿后自动设置视频章节',
    'publish.chapters_translate': '章节标题翻译为中文',
    'publish.chapters_permanent': 'App默认展示章节',
    'publish.chapters_instruction': '章节标题自定义要求',
}


class ConfigurationError(DubForgeError):
    code = 'CONFIGURATION_INVALID'
    suggestion = '请核对设置范围和继承来源后重试。'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def defaults(settings=None):
    from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA
    from dubforge.comment_studio import validate_settings, FILTER_PROMPT
    from dubforge.publishing.service import DEFAULTS
    settings = settings or config.load_config()
    values = {f.path: copy.deepcopy(config.get(f.path, settings=settings))
              for fields in STEP_PARAM_SCHEMA.values() for f in fields}
    values['tts.tolerance'] = config.get('tts.tolerance', .4, settings=settings)
    values.update({'comments.' + k: v for k, v in validate_settings(config.get('comments.overlay', {}, settings=settings)).items()})
    values.update({'publish.' + k: copy.deepcopy(v) for k, v in DEFAULTS.items()})
    values.update({'run.mode': config.get('run.mode', 'draft', settings=settings), 'publish.account_id': '', 'publish.ai_copy': True,
                   'publish.cover_source': 'platform', 'publish.cover_id': '', 'publish.part_titles': {},
                   'comments.mode': 'saved', 'comments.limit': 50, 'comments.count': 10,
                   'comments.prompt': FILTER_PROMPT,
                   'comments.failure_policy': config.get('comments.failure_policy', 'skip', settings=settings),
                   'assembly.versions': [dict(id='dub', name='中文配音版', dubbing=True, burn_subtitles=True,
                                             include_comments=True, overrides={}),
                                         dict(id='src', name='原声双语版', dubbing=False, burn_subtitles=True,
                                              include_comments=True, overrides={'assemble.subtitle_track': 'bilingual'})],
                   'publish.version_ids': None,
                   'assembly.target_version_ids': config.get('assembly.target_version_ids', None, settings=settings),
                   'publish.project_target': None})
    values.update({'publish.' + key: config.get('publish.' + key, True, settings=settings)
                   for key in ('chapters_enabled', 'chapters_translate', 'chapters_permanent')})
    values['publish.chapters_instruction'] = config.get('publish.chapters_instruction', '', settings=settings)
    return values


def _row(session, key):
    row = session.get(ConfigurationScope, key)
    if row is None:
        if key == 'global' or key.startswith('project:'):
            if key.startswith('project:'):
                from dubforge.domain.models import Project
                if session.exec(select(Project).where(Project.uid == key[8:])).first() is None:
                    raise ConfigurationError('项目不存在', code='PROJECT_NOT_FOUND')
            return ConfigurationScope(key=key, name='全局默认' if key == 'global' else '本项目')
        raise ConfigurationError('系列模板不存在', code='CONFIGURATION_NOT_FOUND')
    return row


def scope_source(key, name=''):
    """来源类型与显示名称分离；模板改名或使用保留文案不改变来源身份。"""
    if key.startswith('series:'):
        return dict(kind='series', name=name)
    return dict(kind='project' if key.startswith('project:') else 'global')


def baseline_from(data):
    """只捕获允许的制作与发布字段，明确设置标记独立于完整基础值。"""
    from dubforge.pipeline.step_params import RUNTIME_PARAM_PATHS
    allowed = set(defaults()) - RUNTIME_PARAM_PATHS
    values = {k: copy.deepcopy(v) for k, v in data['values'].items() if k in allowed}
    origins = {k: data.get('origins', {}).get(k, '全局默认') for k in values}
    sources = {}
    for key in values:
        source = data.get('sources', {}).get(key, scope_source('global'))
        sources[key] = {k: copy.deepcopy(v) for k, v in source.items() if k in {'kind', 'name'}}
        sources[key]['frozen'] = True
    # 保留空占位与既有基线补字段迁移兼容，绝不复制本项目的真实原稿目标。
    values['publish.project_target'] = None
    origins['publish.project_target'] = '全局默认'
    sources['publish.project_target'] = {**scope_source('global'), 'frozen': True}
    return dict(schema=1, values=values,
                configured=sorted((set(data.get('configured', [])) & set(values)) - {'publish.project_target'}),
                origins=origins, sources=sources)


def capture_baseline(session, parent='', *, settings=None):
    """模板仍是新项目起点；只有明确应用才替换已有项目基础值。"""
    return baseline_from(_resolve(session, parent or 'global', settings))


def read_baseline(raw, *, settings=None):
    """新字段可使用程序当前缺省；已有字段绝不重新跟随上级。"""
    try:
        data = json.loads(raw) if isinstance(raw, str) else copy.deepcopy(raw)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError('项目基础设置无法读取，请从项目清单找回', code='CONFIGURATION_BASELINE_INVALID') from exc
    if isinstance(data, dict) and not data:
        return None
    if not isinstance(data, dict):
        raise ConfigurationError('项目基础设置须为对象，未使用当前全局值替代', code='CONFIGURATION_BASELINE_INVALID')
    values, configured = data.get('values'), data.get('configured')
    origins, sources = data.get('origins'), data.get('sources')
    if (data.get('schema') != 1 or not isinstance(values, dict) or not isinstance(configured, list)
            or any(not isinstance(k, str) for k in configured) or not isinstance(origins, dict)
            or not isinstance(sources, dict) or not set(configured) <= set(values)):
        raise ConfigurationError('项目基础设置格式无效，请核对项目清单', code='CONFIGURATION_BASELINE_INVALID')
    # 原稿目标不是可继承基础值；兼容历史清单时也不能把别的项目BV带入。
    values.pop('publish.project_target', None)
    configured = [key for key in configured if key != 'publish.project_target']
    from dubforge.pipeline.step_params import RUNTIME_PARAM_PATHS
    allowed = {k: v for k, v in defaults(settings).items() if k not in RUNTIME_PARAM_PATHS}
    if set(values) - set(allowed):
        raise ConfigurationError('项目基础设置含不支持的字段', code='CONFIGURATION_BASELINE_INVALID')
    # 新增排版范围使用程序缺省补齐旧基线，不能借此继承今日已改动的全局范围。
    from dubforge.comment_studio import validate_settings, DEFAULT_SETTINGS
    try:
        old_comments = validate_settings({k: values['comments.' + k] for k in DEFAULT_SETTINGS if 'comments.' + k in values})
        for name in ('min_font_size', 'source_min_font_size', 'height_percent'):
            if 'comments.' + name not in values:
                allowed['comments.' + name] = old_comments[name]
        _validate(values, {**defaults(settings), **allowed}, historical=True, settings=settings)
    except DubForgeError as exc:
        raise ConfigurationError('项目基础设置的参数值无效，请核对项目清单',
                                 code='CONFIGURATION_BASELINE_INVALID') from exc
    for key in values:
        source = sources.get(key)
        if (not isinstance(origins.get(key), str) or not isinstance(source, dict)
                or source.get('kind') not in {'global', 'series'}
                or source.get('kind') == 'series' and not isinstance(source.get('name'), str)):
            raise ConfigurationError('项目基础设置来源无效', code='CONFIGURATION_BASELINE_INVALID')
    # 不接受任意来源扩展字段，避免恢复清单把凭据作为来源公开。
    normalized = dict(values={**allowed, **values}, configured=configured,
                      origins={k: origins.get(k, '全局默认') for k in allowed},
                      sources={k: {**{name: value for name, value in sources.get(k, scope_source('global')).items()
                                     if name in {'kind', 'name'}}, 'frozen': True} for k in allowed})
    return normalized


def _resolve(session, key, settings=None):
    settings = config.load_config() if settings is None else settings
    own = _row(session, key)
    if own.parent and (not key.startswith('project:') or not own.parent.startswith('series:')):
        raise ConfigurationError('设置继承关系无效')
    base = defaults(settings)
    origins = {k: '全局默认' for k in base}
    sources = {k: scope_source('global') for k in base}
    chain = []
    fixed = read_baseline(own.baseline, settings=settings) if key.startswith('project:') else None
    runtime_configured = set()
    if fixed is not None:
        base, origins, sources = copy.deepcopy(fixed['values']), dict(fixed['origins']), copy.deepcopy(fixed['sources'])
        from dubforge.pipeline.step_params import RUNTIME_PARAM_PATHS
        runtime = _resolve(session, own.parent or 'global', settings)
        for path in RUNTIME_PARAM_PATHS & set(runtime['values']):
            base[path] = copy.deepcopy(runtime['values'][path])
            origins[path] = runtime['origins'][path]
            sources[path] = copy.deepcopy(runtime['sources'][path])
        runtime_configured = set(runtime['configured']) & RUNTIME_PARAM_PATHS
    elif key != 'global':
        chain.append(_row(session, 'global'))
    if own.parent and fixed is None:
        parent = _row(session, own.parent)
        if not own.key.startswith('project:') or not parent.key.startswith('series:'):
            raise ConfigurationError('设置继承关系无效')
        chain.append(parent)
    for layer in chain:
        patch = json.loads(layer.values)
        patch.pop('publish.project_target', None)
        base.update(patch)
        origins.update({k: layer.name or '全局默认' for k in patch})
        sources.update({k: scope_source(layer.key, layer.name) for k in patch})
    parent_values, parent_origins = copy.deepcopy(base), dict(origins)
    parent_sources = copy.deepcopy(sources)
    patch = json.loads(own.values)
    if not key.startswith('project:'):
        patch.pop('publish.project_target', None)
    base.update(patch)
    label = '本项目' if key.startswith('project:') else own.name or '全局默认'
    origins.update({k: label for k in patch})
    sources.update({k: scope_source(key, own.name) for k in patch})
    target_selection = None
    if key.startswith('project:'):
        from dubforge.project_publish_target import history_projects, metadata, selection
        selected = selection(patch, has_history=key[8:] in history_projects(session, [key[8:]]))
        base['publish.project_target'] = selected['target']
        target_selection = metadata(selected)
        if selected['source'] == 'first_publish':
            origins['publish.project_target'] = '首次发布默认新稿'
            sources['publish.project_target'] = dict(kind='project', defaulted=True)
    return dict(key=key, name=own.name, parent=own.parent, revision=own.revision, archived=own.archived,
                values=base, overrides=patch, defaults=parent_values, origins=origins,
                configured=sorted(set(patch).union(runtime_configured, fixed['configured'] if fixed is not None else set(),
                    *(set(json.loads(r.values)) for r in chain))),
                parent_origins=parent_origins, sources=sources, parent_sources=parent_sources,
                target_selection=target_selection,
                baseline=dict(schema=1, **copy.deepcopy(fixed)) if fixed is not None else None,
                fingerprint=digest([base, own.parent, fixed, own.revision] if fixed is not None else
                                   [base, [(r.key, r.revision) for r in chain], own.revision]))


def resolve(uid=None, *, key=None, settings=None):
    with Session(get_engine()) as session:
        return _resolve(session, key or ('project:' + uid if uid else 'global'), settings)


def step_values(project, *, settings=None):
    from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA
    paths = {f.path for fields in STEP_PARAM_SCHEMA.values() for f in fields} | {'tts.tolerance'}
    data = resolve(project.uid, settings=settings)
    return {k: v for k, v in data['values'].items() if k in paths}


def schema():
    from dubforge.pipeline.step_params import schema_view
    from dubforge.pipeline.registry import STEP_MAP
    from dubforge.pipeline.assembly_versions import fields
    data = defaults()
    # dubtask 仅是旧工程的兼容任务键；任务准备和逐句试配已经归入
    # “字幕与配音”步骤。它在参数 schema 中保留是为了读取旧覆盖值，
    # 但没有任何可编辑字段，不能作为设置页的空标签显示给用户。
    sections = {key: section for key, section in schema_view(data).items()
                if key != 'dubtask' and section.get('fields')}
    for key, section in sections.items():
        section['label'] = STEP_MAP[key].label if key in STEP_MAP else '内容审查' if key == 'content_review' else key
    comment_fields = [dict(f) for f in fields() if f['path'].startswith('comments.')]
    def field(path, label, type='str', **extra):
        return dict(path=path, label=label, type=type, **extra)
    def option(label, value):
        return dict(label=label, value=value)
    sections['comments'] = dict(label='上屏评论', fields=[
        field('comments.mode', '评论准备', 'select', options=[option('使用已确认的评论', 'saved'), option('自动采集、评分和翻译', 'auto')]),
        field('comments.limit', '最多采集', 'int', min=1, max=500),
        field('comments.count', '最多上屏', 'int', min=1, max=100),
        field('comments.prompt', '评分要求', 'text'),
        field('comments.failure_policy', '个别评论处理失败', 'select', options=[option('跳过并提示，继续其他评论', 'skip'), option('严格暂停', 'stop')]),
        field('comments.start', '首条开始（秒）', 'float', min=0, max=86400),
        field('comments.interval', '出现间隔（秒）', 'float', min=1, max=600),
        field('comments.duration', '每条时长（秒）', 'float', min=1, max=60), *comment_fields])
    sections['publication'] = dict(label='发布规则', fields=[
        field('run.mode', '交付方式', 'select', options=[option('仅保留成片', 'produce'), option('准备发布草稿', 'draft'), option('上传后等待确认', 'upload'), option('上传并提交', 'publish')]),
        field('publish.account_id', 'B站账号', 'select', options=[]),
        field('publish.ai_copy', '自动流程补全文案', 'bool',
              help='仅补充未单独设置的标题、简介、标签和动态；项目或模板中已明确设置的值（包括空值）保持不变。全部四项已设置时不调用 AI。'),
        field('publish.cover_source', '封面来源', 'select', options=[option('平台封面', 'platform'), option('指定封面', 'custom')]),
        field('publish.chapters_enabled', '投稿后自动设置视频章节', 'bool',
              help='有源视频章节时，在审核通过后按各分P实际时间轴和平台规则整理设置；必要时合并相邻主题，无章节则跳过。只补充章节，不重新投稿。'),
        field('publish.chapters_translate', '章节标题翻译为中文', 'bool',
              help='开启时用简体中文改写短标题，关闭时保留原语言。两种模式均校验数量与时长，必要时合并相邻章节并精简标题；时间轴和规则相同的分P复用结果。'),
        field('publish.chapters_permanent', 'App默认展示章节', 'bool',
              help='启用章节同步时生效；控制B站App是否默认显示章节。'),
        field('publish.chapters_instruction', '章节标题自定义要求', 'text',
              help='可自定义内容关注点、标题格式、措辞风格和术语，最多2000字。留空采用默认准确、客观的标题整理与内容审查；平台硬限制不可修改。')])
    return sections


def view(key):
    data = resolve(key=key)
    data['sections'] = schema()
    from dubforge.pipeline.assembly_versions import fields
    data['version_fields'] = list(fields())
    from dubforge.pipeline.step_params import language_capabilities
    data['caps'] = language_capabilities()
    return data


def list_templates():
    with Session(get_engine()) as session:
        rows = session.exec(select(ConfigurationScope).where(ConfigurationScope.key.startswith('series:'))).all()
        return [dict(id=r.key[7:], key=r.key, name=r.name, revision=r.revision, archived=r.archived,
                     project_count=len(session.exec(select(ConfigurationScope).where(ConfigurationScope.parent == r.key)).all()))
                for r in rows]


def _validate(patch, base, *, historical=False, settings=None):
    from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA, validate_numeric_updates
    from dubforge.pipeline.assembly_versions import validate_list
    from dubforge.comment_studio import validate_settings, DEFAULT_SETTINGS
    from dubforge.publishing.service import validate_values, DEFAULTS
    if not isinstance(patch, dict) or set(patch) - set(defaults(settings)):
        raise ConfigurationError('包含不支持的设置字段')
    try:
        _json(patch).encode('utf-8')
    except (ValueError, UnicodeEncodeError) as exc:
        raise ConfigurationError('设置包含无效字符或非有限数值') from exc
    values = {**base, **patch}
    steps = {f.path: f for fields in STEP_PARAM_SCHEMA.values() for f in fields}
    validate_numeric_updates({k: v for k, v in patch.items() if k in steps or k == 'tts.tolerance'})
    from dubforge.pipeline.subtitle_style import validate_style
    validate_style(values)
    from dubforge.pipeline.content_redaction import validate_settings as validate_review
    validate_review(patch)
    for k, v in patch.items():
        f = steps.get(k)
        if f and ((f.type == 'bool' and type(v) is not bool) or (f.type in {'str', 'text', 'password'} and not isinstance(v, str))):
            raise ConfigurationError(f'{f.label}的类型不正确')
        options = [o[0 if k == 'asr.language' else 1] for o in f.options] if f else []
        if f and f.type == 'select' and f.options and not f.editable:
            if type(v) not in {type(option) for option in options}:
                raise ConfigurationError(f'{f.label}的类型不正确')
            # 基线保存已接受的历史值；新版本候选变化不等于旧设置损坏。
            if not historical and v not in options:
                raise ConfigurationError(f'{f.label}请选择有效选项')
        if f and f.type == 'select' and f.editable and not isinstance(v, str):
            raise ConfigurationError(f'{f.label}请填写文字或选择有效选项')
    validate_settings({k: values['comments.' + k] for k in DEFAULT_SETTINGS})
    validate_values({k: values['publish.' + k] for k in DEFAULTS})
    for version in validate_list(values['assembly.versions']):
        if version['burn_subtitles']:
            try:
                validate_style({**values, **version['overrides']})
            except DubForgeError as exc:
                raise ConfigurationError(f'成片版本「{version["name"]}」：{exc.message}',
                                         suggestion=exc.suggestion) from exc
        if version['include_comments']:
            try:
                validate_settings({k: version['overrides'].get('comments.' + k, values['comments.' + k]) for k in DEFAULT_SETTINGS})
            except DubForgeError as exc:
                raise ConfigurationError(f'成片版本「{version["name"]}」：{exc.message}',
                                         suggestion=exc.suggestion) from exc
    for k, limit in [('comments.limit', 500), ('comments.count', 100)]:
        if type(values[k]) is not int or not 1 <= values[k] <= limit:
            raise ConfigurationError('评论数量超出允许范围')
    if values['comments.count'] > values['comments.limit']:
        raise ConfigurationError('上屏数量不能超过采集上限')
    if values['comments.mode'] not in ('auto', 'saved') or values['run.mode'] not in ('produce', 'draft', 'upload', 'publish'):
        raise ConfigurationError('请选择有效的运行或评论准备方式')
    if values['comments.failure_policy'] not in ('skip', 'stop'):
        raise ConfigurationError('请选择有效的评论失败处理方式')
    if values['publish.cover_source'] not in ('platform', 'custom') or type(values['publish.ai_copy']) is not bool:
        raise ConfigurationError('发布规则无效')
    if any(type(values['publish.' + key]) is not bool
           for key in ('chapters_enabled', 'chapters_translate', 'chapters_permanent')):
        raise ConfigurationError('章节开关必须明确选择')
    for k in ['publish.account_id', 'publish.cover_id', 'comments.prompt', 'publish.chapters_instruction']:
        if not isinstance(values[k], str) or len(values[k]) > 2000:
            raise ConfigurationError('发布标识或评论要求格式无效')
    ids = values['publish.version_ids']
    available = {v['id'] for v in values['assembly.versions']}
    if ids is not None and (not isinstance(ids, list) or not ids or any(not isinstance(v, str) for v in ids) or len(ids) != len(set(ids)) or not set(ids) <= available):
        raise ConfigurationError('发布分P引用了不存在或重复的成片版本')
    production = values['assembly.target_version_ids']
    if production is not None and (not isinstance(production, list) or not production
            or any(not isinstance(v, str) for v in production)
            or len(production) != len(set(production)) or not set(production) <= available):
        raise ConfigurationError('项目目标成片引用了不存在或重复的版本，请到合成页重新选择。',
                                 code='PROJECT_ASSEMBLY_TARGET_INVALID')
    target = values['publish.project_target']
    if target is not None:
        if (not isinstance(target, dict) or not isinstance(target.get('kind'), str)
                or target['kind'] not in {'new', 'existing'}):
            raise ConfigurationError('请选择有效的项目发布目标。', code='PROJECT_PUBLISH_TARGET_INVALID')
        if target['kind'] == 'new':
            if set(target) != {'kind'}:
                raise ConfigurationError('新稿目标不能混入原稿更新范围。', code='PROJECT_PUBLISH_TARGET_INVALID')
        else:
            fields = target.get('sync_fields')
            replacements = target.get('replacements')
            if (set(target) != {'kind', 'publication_id', 'replace_videos', 'replacements', 'sync_fields'}
                    or not isinstance(target.get('publication_id'), str) or not 1 <= len(target['publication_id']) <= 100
                    or type(target.get('replace_videos')) is not bool
                    or not isinstance(fields, list) or len(fields) > 100
                    or any(not isinstance(v, str) or not v or len(v) > 100 for v in fields)
                    or len(set(fields)) != len(fields)
                    or not isinstance(replacements, list) or len(replacements) > 100
                    or any(not isinstance(item, dict) or set(item) != {'old_cid', 'version_id'}
                           or type(item['old_cid']) is not int or item['old_cid'] <= 0
                           or not isinstance(item['version_id'], str) or item['version_id'] not in available
                           for item in replacements)
                    or len({item['old_cid'] for item in replacements}) != len(replacements)
                    or len({item['version_id'] for item in replacements}) != len(replacements)):
                raise ConfigurationError('原稿目标或分P替换范围无效，请到发布页重新核对。',
                                         code='PROJECT_PUBLISH_TARGET_INVALID')
    titles = values['publish.part_titles']
    if not isinstance(titles, dict) or set(titles) - available:
        raise ConfigurationError('分P名称引用了不存在的成片版本')
    if any(not isinstance(v, str) or not v.strip() or len(v.encode('utf-16-le')) // 2 > 80 for v in titles.values()):
        raise ConfigurationError('分P名称须为1–80个字符')
    _json(patch)


def _check_idle(session, key, paths=None):
    """只拒绝会改变当前运行步骤执行合同的字段。

    排队任务尚未冻结配置；运行中的其它步骤仍可继续编辑。非配置类调用
    传入 ``None`` 时沿用项目级资源门禁。
    """
    from dubforge import runtime_edit
    structural = {'assembly.target_version_ids', 'publish.project_target'}
    runtime_edit.assert_editable(key, allow_owned=True,
                                paths=None if paths is not None and structural & set(paths) else paths)


def _write_manifests(key):
    from dubforge.domain import crud
    if key.startswith('project:'):
        crud.write_manifest(key[8:])


def save(key, body, *, before_commit=None, on_rollback=None):
    from dubforge.pipeline import dubbing_session
    from dubforge.runner import runner
    with runner._start_lock, dubbing_session.gate, gate, Session(get_engine()) as session:
        row = _row(session, key)
        if row.archived:
            raise ConfigurationError('系列模板已归档，请先恢复')
        if type(body.get('revision')) is not int or body['revision'] != row.revision:
            raise ConfigurationError('设置已在其他页面修改，请刷新后重试', code='CONFIGURATION_CONFLICT')
        current = _resolve(session, key)
        if body.get('fingerprint') and current['fingerprint'] != body['fingerprint']:
            raise ConfigurationError('继承设置已变化，请刷新后核对', code='CONFIGURATION_CONFLICT')
        patch = json.loads(row.values)
        updates, reset = body.get('updates', {}), body.get('reset', [])
        if not isinstance(updates, dict) or not isinstance(reset, list) or any(not isinstance(k, str) or k not in defaults() for k in reset):
            raise ConfigurationError('设置更新格式无效')
        if not key.startswith('project:') and updates.get('publish.project_target') is not None:
            raise ConfigurationError('发布目标只能保存在本项目，不能保存到全局设置或系列模板。',
                                     code='PROJECT_PUBLISH_TARGET_SCOPE')
        if 'assembly.versions' in updates:
            timelines = [v for v in updates['assembly.versions'] if isinstance(v, dict) and v.get('timeline')] if isinstance(updates['assembly.versions'], list) else []
            if timelines and not key.startswith('project:'):
                raise ConfigurationError('成片剪辑引用本项目的媒体，不能保存到全局设置或系列模板。')
            if timelines:
                from dubforge.domain import crud
                from dubforge.pipeline.dependencies import Dependencies
                from dubforge.pipeline.assembly_timeline import load_plan as load_timeline
                timeline_deps = Dependencies(crud.get_project(key[8:]))
                for version in timelines:
                    load_timeline(timeline_deps, version)
            partials = [v for v in updates['assembly.versions'] if isinstance(v, dict) and v.get('partial')] if isinstance(updates['assembly.versions'], list) else []
            if partials and not key.startswith('project:'):
                raise ConfigurationError('局部修订引用本项目的成片，不能保存到全局设置或系列模板。')
            if partials:
                from dubforge.domain import crud
                from dubforge.pipeline.dependencies import Dependencies
                from dubforge.pipeline.assembly_partial import load_plan
                deps = Dependencies(crud.get_project(key[8:]))
                for version in partials:
                    load_plan(deps, version)
        if 'assembly.versions' in updates or 'assembly.versions' in reset:
            from dubforge.project_batches import assert_version_goals
            assert_version_goals(key,current['values']['assembly.versions'],
                updates.get('assembly.versions',current['defaults']['assembly.versions']))
        _check_idle(session, key, set(updates) | set(reset))
        patch.update(updates)
        for k in reset:
            patch.pop(k, None)
        _validate(patch, current['defaults'])
        target = patch.get('publish.project_target')
        if isinstance(target, dict) and target.get('kind') == 'existing':
            from dubforge.publishing.models import Publication
            publication = session.get(Publication, target['publication_id'])
            if publication is None or publication.project_uid != key[8:] or publication.deleted_at is not None:
                raise ConfigurationError('所选原稿不属于本项目或已被移除，请到发布页重新选择。',
                                         code='PROJECT_PUBLISH_TARGET_INVALID')
        row.values, row.revision = _json(patch), row.revision + 1
        if key.startswith('series:') and 'name' in body:
            name = body['name']
            if not isinstance(name, str) or not name.strip() or len(name) > 80:
                raise ConfigurationError('请填写80字以内的模板名称')
            row.name = name.strip()
        session.add(row)
        session.flush()
        # 全局仍是模板的起点；项目基础值已独立，不校验它与最新模板的组合。
        if key == 'global':
            children = session.exec(select(ConfigurationScope).where(ConfigurationScope.key.startswith('series:'))).all()
            for child in children:
                effective = _resolve(session, child.key)
                _validate(effective['overrides'], effective['defaults'])
        try:
            if before_commit is not None:
                before_commit()
            session.commit()
        except Exception:
            session.rollback()
            if on_rollback is not None:
                on_rollback()
            raise
    _write_manifests(key)
    return view(key)


def create_template(name):
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ConfigurationError('请填写80字以内的模板名称')
    row = ConfigurationScope(key='series:' + uuid.uuid4().hex, name=name.strip())
    with gate, Session(get_engine()) as session:
        session.add(row); session.commit(); key = row.key
    return view(key)


def archive(key, body):
    if not key.startswith('series:') or type(body.get('archived')) is not bool:
        raise ConfigurationError('只能归档或恢复系列模板')
    from dubforge.runner import runner
    from dubforge.pipeline import dubbing_session
    with runner._start_lock, dubbing_session.gate, gate, Session(get_engine()) as session:
        row = _row(session, key)
        if body.get('revision') != row.revision:
            raise ConfigurationError('模板已变化，请刷新后重试', code='CONFIGURATION_CONFLICT')
        _check_idle(session, key)
        row.archived = body['archived']; row.revision += 1
        session.add(row); session.commit()
    return view(key)


def binding_plan(uids, template_id):
    if not isinstance(uids, list) or not uids or len(uids) > 100 or any(not isinstance(v,str) for v in uids) or len(set(uids)) != len(uids):
        raise ConfigurationError('请选择1–100个不重复的项目')
    if not isinstance(template_id, str):
        raise ConfigurationError('模板标识无效')
    parent = 'series:' + template_id if template_id else ''
    with Session(get_engine()) as session:
        template = _row(session, parent) if parent else None
        if template and template.archived:
            raise ConfigurationError('不能绑定已归档模板')
        source = _resolve(session, parent or 'global')
        fields = {f['path']: f for section in schema().values() for f in section['fields']}
        from dubforge.pipeline.registry import STEP_MAP
        from dubforge.pipeline.step_params import RUNTIME_PARAM_PATHS
        from dubforge.domain.models import Project
        from dubforge.pipeline.config_fingerprint import TEXT_EXECUTION_FIELDS, EXECUTION_ONLY_PATHS
        execution_paths = {step + '.' + name for step, names in TEXT_EXECUTION_FIELDS.items() for name in names} | EXECUTION_ONLY_PATHS
        titles = dict(session.exec(select(Project.uid, Project.title).where(Project.uid.in_(uids))).all())
        def display(path, value):
            field = fields.get(path, {})
            private = (field.get('type') in {'text', 'password'}
                       or path.endswith(('_path', '_audio', '.url', '.project_dir', '.proxy')))
            if private or isinstance(value, (dict, list)):
                return '（未设置）' if value in ('', None, [], {}) else '已设置 · 摘要 ' + digest(value)[:10]
            if isinstance(value, str):
                return value[:120] + ('…' if len(value) > 120 else '')
            return value
        rows = []
        for uid in uids:
            data = _resolve(session, 'project:' + uid)
            base = source['values']
            effective = {**base, **data['overrides']}
            _validate({}, effective)
            changes = [k for k in effective if effective[k] != data['values'][k]]
            affected = {step for step, definition in STEP_MAP.items()
                        if any(path == key or path.startswith(key + '.') for path in changes
                               if path not in RUNTIME_PARAM_PATHS and path not in execution_paths
                               for key in definition.cfg_keys)}
            if 'assembly.versions' in changes:
                affected.add('assemble')
            if any(path.startswith('comments.') for path in changes):
                affected.update({'comments', 'assemble'})
            details = [dict(path=path, label=fields.get(path, {}).get('label',
                               _BINDING_LABELS.get(path, '高级设置' if path.startswith('publish.') else path)),
                            before=display(path, data['values'][path]), after=display(path, effective[path]))
                       for path in changes]
            rows.append(dict(uid=uid, title=titles.get(uid, uid), previous=data['parent'], revision=data['revision'],
                             # 差异预览可按对应账号读取平台名称；这里只传账号标识，
                             # 不在绑定计划阶段发起远程请求，避免预览被B站网络阻塞。
                             publication_context=dict(
                                 before=dict(
                                     account_id=data['values'].get('publish.account_id') or '',
                                     season_id=data['values'].get('publish.season_id'),
                                 ),
                                 after=dict(
                                     account_id=effective.get('publish.account_id') or '',
                                     season_id=effective.get('publish.season_id'),
                                 ),
                             ),
                             preserved=len(data['overrides']), changes=changes,
                             change_details=details, affected_steps=[k for k in [*STEP_MAP, 'comments'] if k in affected],
                             fingerprint=data['fingerprint']))
        return dict(projects=rows, template_id=template_id, name=template.name if template else '全局默认',
                    source_fingerprint=source['fingerprint'],
                    fingerprint=digest([rows, parent, source['fingerprint']]))


def bind(body):
    from dubforge.runner import runner
    from dubforge.pipeline import dubbing_session
    with runner._start_lock, dubbing_session.gate, gate:
        plan = binding_plan(body.get('uids'), body.get('template_id', ''))
        if body.get('confirmed') is not True or body.get('fingerprint') != plan['fingerprint']:
            raise ConfigurationError('请先核对模板绑定影响，再确认', code='CONFIGURATION_CONFLICT')
        with Session(get_engine()) as session:
            for item in plan['projects']:
                key = 'project:' + item['uid']
                _check_idle(session, key)
                row = _row(session, key)
                row.parent = 'series:' + body['template_id'] if body.get('template_id') else ''
                row.baseline = _json(capture_baseline(session, row.parent))
                row.revision += 1
                session.add(row)
            session.commit()
    for item in plan['projects']:
        _write_manifests('project:' + item['uid'])
    return plan


def run_spec(uid, *, mode=None, target_driven=False):
    """从同一设置推导执行输入；运行检查没有第二份可编辑配方。"""
    from dubforge.comment_studio import DEFAULT_SETTINGS
    from dubforge.publishing.service import DEFAULTS
    data = resolve(uid)
    v = data['values']
    version_ids = v['publish.version_ids'] if v['publish.version_ids'] is not None else [i['id'] for i in v['assembly.versions']]
    result = dict(mode=v['run.mode'] if target_driven else mode or v['run.mode'], variants=['dub_hardsub_comments'],
                version_ids=version_ids,
                part_titles={k:title for k,title in v['publish.part_titles'].items() if k in version_ids}, account_id=v['publish.account_id'], template_id='',
                overrides={k: v['publish.' + k] for k in DEFAULTS if 'publish.' + k in data['configured']},
                cover_source=v['publish.cover_source'], cover_id=v['publish.cover_id'], ai_copy=v['publish.ai_copy'],
                chapters_enabled=v['publish.chapters_enabled'], chapters_translate=v['publish.chapters_translate'],
                chapters_permanent=v['publish.chapters_permanent'],
                chapters_instruction=v['publish.chapters_instruction'],
                comments_mode=v['comments.mode'], comment_limit=v['comments.limit'],
                comment_count=v['comments.count'], comment_prompt=v['comments.prompt'],
                comment_failure_policy=v['comments.failure_policy'],
                comment_settings={k: v['comments.' + k] for k in DEFAULT_SETTINGS})
    if target_driven:
        from dubforge.project_execution import bind_recipe
        return bind_recipe(result, data)
    return result


def batch_plan(body):
    from dubforge import workflows
    from dubforge.domain import crud
    uids = body.get('uids')
    if not isinstance(uids, list) or not uids or len(uids) > 100 or any(not isinstance(v,str) for v in uids) or len(set(uids)) != len(uids):
        raise ConfigurationError('请选择1–100个不重复的项目')
    items = []
    for uid in uids:
        project = crud.get_project(uid)
        try:
            intent = ({'target_driven': True} if body.get('target_driven') is True
                      else dict(mode=body.get('mode')))
            if 'dubbing' in body:
                intent['dubbing'] = body['dubbing']
            plan = workflows.preview(uid, intent)
            items.append(dict(uid=uid, title=project.title, plan=plan, blockers=plan['blockers']))
        except DubForgeError as exc:
            items.append(dict(uid=uid, title=project.title if project else uid, plan=None, blockers=[exc.message]))
    return dict(items=items)


def batch_start(body):
    from dubforge import workflows
    if body.get('confirmed') is not True or not isinstance(body.get('items'), list) or not 1 <= len(body['items']) <= 100:
        raise ConfigurationError('请先检查批量运行范围并确认')
    try:
        request = uuid.UUID(body['request_id'])
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ConfigurationError('批量请求标识无效')
    results = []
    seen = set()
    for item in body['items']:
        uid = item.get('uid') if isinstance(item, dict) else None
        if not isinstance(uid, str) or uid in seen:
            raise ConfigurationError('批量项目标识重复或无效')
        seen.add(uid)
    for item in body['items']:
        uid = item['uid']
        try:
            recipe = item.get('recipe')
            if body.get('target_driven') is True:
                recipe = {**(recipe if isinstance(recipe, dict) else {}), 'target_driven': True}
            run = workflows.start(uid, dict(recipe=recipe, fingerprint=item.get('fingerprint'),
                confirmed=True, request_id=str(uuid.uuid5(request, uid))))
            results.append(dict(uid=uid, run_id=run['id'], error=''))
        except DubForgeError as exc:
            results.append(dict(uid=uid, error=exc.message))
        except Exception:
            logger.exception('批量运行项目 %s 启动失败', uid)
            error = ConfigurationError('项目启动失败，请查看后台日志后重试', code='BATCH_START_FAILED')
            results.append(dict(uid=uid, error=error.message, code=error.code))
    return dict(items=results)
