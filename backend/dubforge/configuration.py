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
    values.update({'run.mode': 'draft', 'publish.account_id': '', 'publish.ai_copy': True,
                   'publish.cover_source': 'platform', 'publish.cover_id': '', 'publish.part_titles': {},
                   'comments.mode': 'saved', 'comments.limit': 50, 'comments.count': 10,
                   'comments.prompt': FILTER_PROMPT,
                   'comments.failure_policy': config.get('comments.failure_policy', 'skip', settings=settings),
                   'assembly.versions': [dict(id='dub', name='中文配音版', dubbing=True, burn_subtitles=True,
                                             include_comments=True, overrides={}),
                                         dict(id='src', name='原声双语版', dubbing=False, burn_subtitles=True,
                                              include_comments=True, overrides={'assemble.subtitle_track': 'bilingual'})],
                   'publish.version_ids': None})
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


def _resolve(session, key, settings=None):
    own = _row(session, key)
    base = defaults(settings)
    origins = {k: '全局默认' for k in base}
    sources = {k: scope_source('global') for k in base}
    chain = []
    if key != 'global':
        chain.append(_row(session, 'global'))
    if own.parent:
        parent = _row(session, own.parent)
        if not own.key.startswith('project:') or not parent.key.startswith('series:'):
            raise ConfigurationError('设置继承关系无效')
        chain.append(parent)
    for layer in chain:
        patch = json.loads(layer.values)
        base.update(patch)
        origins.update({k: layer.name or '全局默认' for k in patch})
        sources.update({k: scope_source(layer.key, layer.name) for k in patch})
    parent_values, parent_origins = copy.deepcopy(base), dict(origins)
    parent_sources = copy.deepcopy(sources)
    patch = json.loads(own.values)
    base.update(patch)
    label = '本项目' if key.startswith('project:') else own.name or '全局默认'
    origins.update({k: label for k in patch})
    sources.update({k: scope_source(key, own.name) for k in patch})
    return dict(key=key, name=own.name, parent=own.parent, revision=own.revision, archived=own.archived,
                values=base, overrides=patch, defaults=parent_values, origins=origins,
                configured=sorted(set(patch).union(*(set(json.loads(r.values)) for r in chain))),
                parent_origins=parent_origins, sources=sources, parent_sources=parent_sources,
                fingerprint=digest([base, [(r.key, r.revision) for r in chain], own.revision]))


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
    sections = schema_view(data)
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
        field('run.mode', '执行终点', 'select', options=[option('仅制作成片', 'produce'), option('准备发布草稿', 'draft'), option('自动投稿', 'publish')]),
        field('publish.account_id', 'B站账号', 'select', options=[]),
        field('publish.ai_copy', '自动流程补全文案', 'bool',
              help='仅补充未单独设置的标题、简介、标签和动态；项目或模板中已明确设置的值（包括空值）保持不变。全部四项已设置时不调用 AI。'),
        field('publish.cover_source', '封面来源', 'select', options=[option('平台封面', 'platform'), option('指定封面', 'custom')])])
    return sections


def view(key):
    data = resolve(key=key)
    data['sections'] = schema()
    from dubforge.pipeline.assembly_versions import fields
    data['version_fields'] = list(fields())
    from dubforge.pipeline.step_params import language_capabilities
    from dubforge.backends.tts_indextts25 import synthesis_profiles
    data['caps'] = language_capabilities()
    data['indextts_profiles'] = synthesis_profiles()
    return data


def list_templates():
    with Session(get_engine()) as session:
        rows = session.exec(select(ConfigurationScope).where(ConfigurationScope.key.startswith('series:'))).all()
        return [dict(id=r.key[7:], key=r.key, name=r.name, revision=r.revision, archived=r.archived,
                     project_count=len(session.exec(select(ConfigurationScope).where(ConfigurationScope.parent == r.key)).all()))
                for r in rows]


def _validate(patch, base):
    from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA, validate_numeric_updates
    from dubforge.pipeline.assembly_versions import validate_list
    from dubforge.comment_studio import validate_settings, DEFAULT_SETTINGS
    from dubforge.publishing.service import validate_values, DEFAULTS
    if not isinstance(patch, dict) or set(patch) - set(defaults()):
        raise ConfigurationError('包含不支持的设置字段')
    try:
        _json(patch).encode('utf-8')
    except (ValueError, UnicodeEncodeError) as exc:
        raise ConfigurationError('设置包含无效字符或非有限数值') from exc
    values = {**base, **patch}
    steps = {f.path: f for fields in STEP_PARAM_SCHEMA.values() for f in fields}
    validate_numeric_updates({k: v for k, v in patch.items() if k in steps or k == 'tts.tolerance'})
    from dubforge.pipeline.content_redaction import validate_settings as validate_review
    validate_review(patch)
    for k, v in patch.items():
        f = steps.get(k)
        if f and ((f.type == 'bool' and type(v) is not bool) or (f.type in {'str', 'text', 'password'} and not isinstance(v, str))):
            raise ConfigurationError(f'{f.label}的类型不正确')
        if f and f.type == 'select' and f.options and not f.editable and v not in [o[1] for o in f.options]:
            raise ConfigurationError(f'{f.label}请选择有效选项')
        if f and f.type == 'select' and f.editable and not isinstance(v, str):
            raise ConfigurationError(f'{f.label}请填写文字或选择有效选项')
    validate_settings({k: values['comments.' + k] for k in DEFAULT_SETTINGS})
    validate_values({k: values['publish.' + k] for k in DEFAULTS})
    validate_list(values['assembly.versions'])
    for k, limit in [('comments.limit', 500), ('comments.count', 100)]:
        if type(values[k]) is not int or not 1 <= values[k] <= limit:
            raise ConfigurationError('评论数量超出允许范围')
    if values['comments.count'] > values['comments.limit']:
        raise ConfigurationError('上屏数量不能超过采集上限')
    if values['comments.mode'] not in ('auto', 'saved') or values['run.mode'] not in ('produce', 'draft', 'publish'):
        raise ConfigurationError('请选择有效的运行或评论准备方式')
    if values['comments.failure_policy'] not in ('skip', 'stop'):
        raise ConfigurationError('请选择有效的评论失败处理方式')
    if values['publish.cover_source'] not in ('platform', 'custom') or type(values['publish.ai_copy']) is not bool:
        raise ConfigurationError('发布规则无效')
    for k in ['publish.account_id', 'publish.cover_id', 'comments.prompt']:
        if not isinstance(values[k], str) or len(values[k]) > 2000:
            raise ConfigurationError('发布标识或评论要求格式无效')
    ids = values['publish.version_ids']
    available = {v['id'] for v in values['assembly.versions']}
    if ids is not None and (not isinstance(ids, list) or not ids or any(not isinstance(v, str) for v in ids) or len(ids) != len(set(ids)) or not set(ids) <= available):
        raise ConfigurationError('发布分P引用了不存在或重复的成片版本')
    titles = values['publish.part_titles']
    if not isinstance(titles, dict) or set(titles) - available:
        raise ConfigurationError('分P名称引用了不存在的成片版本')
    if any(not isinstance(v, str) or not v.strip() or len(v.encode('utf-16-le')) // 2 > 80 for v in titles.values()):
        raise ConfigurationError('分P名称须为1–80个字符')
    _json(patch)


def _check_idle(session, key):
    """运行期间禁止外部编辑；冻结快照与步骤边界指纹仍作为恢复保护。"""
    from dubforge import runtime_edit
    runtime_edit.assert_editable(key, allow_owned=True)


def _write_manifests(key):
    from dubforge.domain import crud
    from dubforge.domain.models import Project
    with Session(get_engine()) as session:
        if key.startswith('project:'):
            uids = [key[8:]]
        elif key.startswith('series:'):
            uids = [r.key[8:] for r in session.exec(select(ConfigurationScope).where(ConfigurationScope.parent == key)).all()]
        else:
            uids = [p.uid for p in session.exec(select(Project)).all()]
    for uid in uids:
        crud.write_manifest(uid)


def save(key, body, *, before_commit=None, on_rollback=None):
    from dubforge.pipeline import dubbing_session
    from dubforge.runner import runner
    with runner._start_lock, dubbing_session.gate, gate, Session(get_engine()) as session:
        row = _row(session, key)
        if row.archived:
            raise ConfigurationError('系列模板已归档，请先恢复')
        if type(body.get('revision')) is not int or body['revision'] != row.revision:
            raise ConfigurationError('设置已在其他页面修改，请刷新后重试', code='CONFIGURATION_CONFLICT')
        _check_idle(session, key)
        current = _resolve(session, key)
        if body.get('fingerprint') and current['fingerprint'] != body['fingerprint']:
            raise ConfigurationError('继承设置已变化，请刷新后核对', code='CONFIGURATION_CONFLICT')
        patch = json.loads(row.values)
        updates, reset = body.get('updates', {}), body.get('reset', [])
        if not isinstance(updates, dict) or not isinstance(reset, list) or any(not isinstance(k, str) or k not in defaults() for k in reset):
            raise ConfigurationError('设置更新格式无效')
        patch.update(updates)
        for k in reset:
            patch.pop(k, None)
        _validate(patch, current['defaults'])
        row.values, row.revision = _json(patch), row.revision + 1
        if key.startswith('series:') and 'name' in body:
            name = body['name']
            if not isinstance(name, str) or not name.strip() or len(name) > 80:
                raise ConfigurationError('请填写80字以内的模板名称')
            row.name = name.strip()
        session.add(row)
        session.flush()
        # 父层修改也必须保持已绑定项目有效，例如不能删掉项目正在引用的分P版本。
        if not key.startswith('project:'):
            children = session.exec(select(ConfigurationScope)).all()
            for child in children:
                if child.key != key and (key == 'global' or child.parent == key):
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
        rows = []
        for uid in uids:
            data = _resolve(session, 'project:' + uid)
            base = _resolve(session, parent or 'global')['values']
            effective = {**base, **data['overrides']}
            _validate({}, effective)
            rows.append(dict(uid=uid, previous=data['parent'], revision=data['revision'],
                             preserved=len(data['overrides']), changes=[k for k in effective if effective[k] != data['values'][k]],
                             fingerprint=data['fingerprint']))
        return dict(projects=rows, template_id=template_id, name=template.name if template else '全局默认',
                    fingerprint=digest([rows, parent, template.revision if template else _row(session, 'global').revision]))


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
                row.revision += 1
                session.add(row)
            session.commit()
    for item in plan['projects']:
        _write_manifests('project:' + item['uid'])
    return plan


def run_spec(uid, *, mode=None):
    """从同一设置推导执行输入；运行检查没有第二份可编辑配方。"""
    from dubforge.comment_studio import DEFAULT_SETTINGS
    from dubforge.publishing.service import DEFAULTS
    data = resolve(uid)
    v = data['values']
    version_ids = v['publish.version_ids'] if v['publish.version_ids'] is not None else [i['id'] for i in v['assembly.versions']]
    return dict(mode=mode or v['run.mode'], variants=['dub_hardsub_comments'],
                version_ids=version_ids,
                part_titles={k:title for k,title in v['publish.part_titles'].items() if k in version_ids}, account_id=v['publish.account_id'], template_id='',
                overrides={k: v['publish.' + k] for k in DEFAULTS if 'publish.' + k in data['configured']},
                cover_source=v['publish.cover_source'], cover_id=v['publish.cover_id'], ai_copy=v['publish.ai_copy'],
                comments_mode=v['comments.mode'], comment_limit=v['comments.limit'],
                comment_count=v['comments.count'], comment_prompt=v['comments.prompt'],
                comment_failure_policy=v['comments.failure_policy'],
                comment_settings={k: v['comments.' + k] for k in DEFAULT_SETTINGS})


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
            plan = workflows.preview(uid, dict(mode=body.get('mode')))
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
            run = workflows.start(uid, dict(recipe=item.get('recipe'), fingerprint=item.get('fingerprint'),
                confirmed=True, request_id=str(uuid.uuid5(request, uid))))
            results.append(dict(uid=uid, run_id=run['id'], error=''))
        except DubForgeError as exc:
            results.append(dict(uid=uid, error=exc.message))
        except Exception:
            logger.exception('批量运行项目 %s 启动失败', uid)
            error = ConfigurationError('项目启动失败，请查看后台日志后重试', code='BATCH_START_FAILED')
            results.append(dict(uid=uid, error=error.message, code=error.code))
    return dict(items=results)
