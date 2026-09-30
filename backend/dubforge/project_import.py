"""批量项目导入：只解析和创建，不联网抓标题、不下载、不启动流程。"""
import copy
import json
import os
import re
import threading
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from sqlmodel import Session

from dubforge import configuration as cfg
from dubforge.configuration_models import ConfigurationScope
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.video_source import _VIDEO_EXT
from dubforge.project_import_models import ProjectImport

SCHEMA = 'dubforge.batch.v1'
MAX_ITEMS = 100
MAX_BYTES = 1024 * 1024
gate = threading.RLock()
# 连接地址和凭据文件由环境设置管理，不接受外部 LLM 文本改写。
ENVIRONMENT_FIELDS = {'download.proxy', 'download.cookies_path', 'translate.custom_terms_path',
                      'tts.indextts25.url', 'tts.gptsovits.url'}


class ImportError(DubForgeError):
    code = 'PROJECT_IMPORT_INVALID'
    suggestion = '请修改标出的条目并重新预览；导入只创建项目，不会启动制作或投稿。'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def allowed_settings():
    return set(cfg.defaults()) - ENVIRONMENT_FIELDS


def _settings(value):
    if not isinstance(value, dict):
        raise ImportError('settings 必须是参数对象')
    unknown = set(value) - allowed_settings()
    if unknown:
        raise ImportError('不支持的导入参数：' + '、'.join(sorted(unknown)))
    return copy.deepcopy(value)


def _keys(value, allowed, label):
    if not isinstance(value, dict):
        raise ImportError(f'{label}必须是对象')
    if set(value) - allowed:
        raise ImportError(f'{label}包含未知字段：' + '、'.join(sorted(set(value) - allowed)))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImportError(f'JSON 中的字段重复：{key}')
        result[key] = value
    return result


def _constant(value):
    raise ImportError(f'JSON 不支持数值 {value}')


def parse(text):
    if not isinstance(text, str):
        raise ImportError('导入内容必须是文本')
    try:
        size = len(text.encode('utf-8'))
    except UnicodeEncodeError as exc:
        raise ImportError('导入文本包含无效字符，请使用 UTF-8 编码') from exc
    if size > MAX_BYTES:
        raise ImportError('导入文本不能超过1MB')
    text = text.lstrip('\ufeff').strip()
    if text.startswith('```'):
        match = re.fullmatch(r'```(?:json|text|txt)?\s*\n([\s\S]*?)\n```', text, re.I)
        if not match:
            raise ImportError('请只粘贴完整 JSON 或逐行地址，不要包含额外说明')
        text = match[1].strip()
    if text.startswith(('{', '[')):
        try:
            document = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
            _json(document).encode('utf-8')
        except json.JSONDecodeError as exc:
            raise ImportError(f'JSON 第{exc.lineno}行第{exc.colno}列格式错误；请检查引号、逗号和路径转义') from exc
        except RecursionError as exc:
            raise ImportError('JSON 嵌套层级过深') from exc
        except ValueError as exc:
            raise ImportError('JSON 包含无效字符或超出范围的数值') from exc
        _keys(document, {'schema', 'defaults', 'projects'}, '导入清单')
        if document.get('schema') != SCHEMA:
            raise ImportError(f'schema 必须为 {SCHEMA}')
        defaults = document.get('defaults', {})
        _keys(defaults, {'template', 'settings'}, '批次默认项')
        _settings(defaults.get('settings', {}))
        rows = document.get('projects')
    else:
        defaults = {}
        rows = [dict(source=line.strip()) for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith('#')]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ITEMS:
        raise ImportError(f'每批请提供1–{MAX_ITEMS}个视频')
    return defaults, rows


def source(value, *, check_file=True):
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise ImportError('请填写有效的视频链接或绝对路径')
    value = value.strip().strip('"')
    if any(ord(c) < 32 for c in value):
        raise ImportError('视频地址不能包含换行或控制字符')
    if re.match(r'https?://', value, re.I):
        try:
            parts = urlsplit(value)
            if not parts.hostname or parts.username or parts.password or any(c.isspace() for c in value):
                raise ValueError()
            port = parts.port
        except ValueError as exc:
            raise ImportError('视频链接无效，且不能包含账号密码') from exc
        host = parts.hostname.lower()
        query = parse_qs(parts.query)
        video = ''
        if host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be'}:
            segments = parts.path.strip('/').split('/')
            video = (segments[0] if host.endswith('youtu.be') else query.get('v', [''])[0]
                     or (segments[1] if len(segments) == 2 and segments[0] in {'shorts', 'embed', 'live'} else ''))
            if not video:
                raise ImportError('请提供单个 YouTube 视频地址；暂不展开频道或播放列表')
        normalized_host = host + (f':{port}' if port and port not in {80, 443} else '')
        identity = ('youtube:' + video if video else
                    urlunsplit((parts.scheme.lower(), normalized_host, parts.path or '/', parts.query, '')))
        title = (host + '_' + (video or parts.path.rstrip('/').rsplit('/', 1)[-1])).rstrip('_')
        return dict(source=value, source_kind='link', identity=identity, title=title[:200])
    path = Path(value)
    if not path.is_absolute() or value.startswith(('\\\\', '//', '\\\\?\\', '\\\\.\\')):
        raise ImportError('本地视频请填写本机磁盘绝对路径，不接受相对路径、共享或设备路径')
    if path.suffix.lower() not in _VIDEO_EXT:
        raise ImportError('本地路径必须指向 mp4、mkv、mov、webm 等视频文件')
    if check_file and not path.is_file():
        raise ImportError('本地视频文件不存在；仅引用原文件，不会上传或复制')
    resolved = path.resolve()
    return dict(source=str(resolved), source_kind='local', identity='local:' + os.path.normcase(str(resolved)), title=path.stem[:200])


def _template(value, templates):
    if value is None or value == '':
        return '', '全局默认'
    if not isinstance(value, str):
        raise ImportError('template 请填写模板名称、series:标识或 null')
    matches = ([t for t in templates if t['key'] == value] if value.startswith('series:') else
               [t for t in templates if t['name'] == value])
    if len(matches) != 1:
        raise ImportError('模板不存在或名称不唯一，请使用样板中的 series:标识')
    item = matches[0]
    if item['archived']:
        raise ImportError('模板已归档，请选择其他模板或先恢复')
    return item['key'], item['name']


def preview(body):
    _keys(body, {'text', 'default_template', 'template_overrides'}, '导入请求')
    defaults, raw_rows = parse(body.get('text'))
    overrides = body.get('template_overrides', {})
    if not isinstance(overrides, dict) or any(k not in {str(i) for i in range(len(raw_rows))} for k in overrides):
        raise ImportError('逐行模板选择无效，请重新预览')
    templates = cfg.list_templates()
    existing = {}
    for project in crud.list_projects():
        try:
            identity = source(project.source_url, check_file=False)['identity']
        except (DubForgeError, OSError, ValueError):
            continue
        existing.setdefault(identity, []).append(dict(uid=project.uid, title=project.title))
    rows, seen, bases = [], {}, {}
    for index, raw in enumerate(raw_rows):
        row = dict(index=index, source='', title='', template='', template_name='', settings={},
                   errors=[], warnings=[], duplicates=[], selectable=False)
        try:
            _keys(raw, {'source', 'title', 'template', 'settings'}, f'第{index + 1}项')
            row['source'] = raw.get('source') if isinstance(raw.get('source'), str) else ''
            row.update(source(raw.get('source')))
            title = raw.get('title', '')
            if not isinstance(title, str) or len(title) > 200:
                raise ImportError('项目名称请填写200字以内的文字')
            row['title'] = title.strip() or row['title']
            selected = overrides.get(str(index), raw.get('template', defaults.get('template', body.get('default_template'))))
            parent, name = _template(selected, templates)
            row['template'], row['template_name'] = parent, name
            if parent not in bases:
                bases[parent] = cfg.resolve(key=parent or 'global')
            base = bases[parent]
            patch = {**_settings(defaults.get('settings', {})), **_settings(raw.get('settings', {}))}
            cfg._validate(patch, base['values'])
            row['settings'], row['base_fingerprint'] = patch, base['fingerprint']
            row['duplicates'] = existing.get(row['identity'], [])
            if row['duplicates']:
                row['warnings'].append('项目空间已有相同来源，默认不勾选；仍需新建时可手动勾选')
            if row['identity'] in seen:
                row['warnings'].append(f'与第{seen[row["identity"]] + 1}项来源重复，默认不勾选')
            else:
                seen[row['identity']] = index
            row['duplicate'] = bool(row['warnings'])
            row['selectable'] = True
        except (DubForgeError, OSError, ValueError, TypeError) as exc:
            row['errors'].append(exc.message if isinstance(exc, DubForgeError) else '参数或本地路径无效，请检查后重试')
        rows.append(row)
    return dict(items=rows, fingerprint=cfg.digest(rows), total=len(rows),
                selected=[r['index'] for r in rows if r['selectable'] and not r['duplicate']])


def _result(row):
    plan = json.loads(row.plan)
    saved = json.loads(row.results)
    items = []
    for item in plan:
        result = saved.get(str(item['index']), dict(status='pending', uid='', error=''))
        if result['status'] == 'created' and crud.get_project(result['uid']) is None:
            result = {**result, 'status': 'removed', 'error': '项目后来已删除，不会因重试重新创建'}
        items.append(dict(index=item['index'], title=item['title'], source=item['source'],
                          template=item['template'], settings=item['settings'], **result))
    return dict(request_id=row.id, items=items, created=sum(i['status'] == 'created' for i in items),
                complete=all(i['status'] in {'created', 'removed'} for i in items))


def status(request_id):
    with Session(get_engine()) as session:
        row = session.get(ProjectImport, request_id)
        if not row:
            raise ImportError('没有此批导入记录', code='PROJECT_IMPORT_NOT_FOUND')
        return _result(row)


def _record(request_id, index, result):
    with Session(get_engine()) as session:
        row = session.get(ProjectImport, request_id)
        results = json.loads(row.results)
        results[str(index)] = result
        row.results = _json(results)
        session.add(row)
        session.commit()


def execute(body):
    _keys(body, {'input', 'selected', 'fingerprint', 'request_id', 'confirmed'}, '创建请求')
    if body.get('confirmed') is not True:
        raise ImportError('请先预览并确认要创建的项目')
    try:
        request_id = str(uuid.UUID(body.get('request_id', '')))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ImportError('批次请求标识无效') from exc
    selected = body.get('selected')
    if (not isinstance(selected, list) or not 1 <= len(selected) <= MAX_ITEMS
            or any(type(i) is not int or i < 0 for i in selected) or len(set(selected)) != len(selected)):
        raise ImportError('请勾选1–100个有效条目')
    _keys(body.get('input'), {'text', 'default_template', 'template_overrides'}, '导入请求')
    parse(body['input'].get('text'))
    try:
        input_hash = cfg.digest({k: body.get(k) for k in ('input', 'selected', 'fingerprint')})
    except (ValueError, TypeError, RecursionError) as exc:
        raise ImportError('创建请求的参数格式无效') from exc
    with gate:
        with cfg.gate, Session(get_engine()) as session:
            row = session.get(ProjectImport, request_id)
            if row:
                if row.input_hash != input_hash:
                    raise ImportError('此批次的内容已改变，请重新预览', code='PROJECT_IMPORT_CONFLICT')
            else:
                plan = preview(body.get('input'))
                if body.get('fingerprint') != plan['fingerprint']:
                    raise ImportError('模板、来源或现有项目已变化，请重新预览', code='PROJECT_IMPORT_CONFLICT')
                if any(i >= len(plan['items']) or not plan['items'][i]['selectable'] for i in selected):
                    raise ImportError('不能创建未通过校验的条目')
                row = ProjectImport(id=request_id, input_hash=input_hash,
                                    plan=_json([plan['items'][i] for i in selected]))
                session.add(row)
                session.commit()
            items = json.loads(row.plan)
        for item in items:
            with Session(get_engine()) as session:
                saved = json.loads(session.get(ProjectImport, request_id).results).get(str(item['index']))
            if saved and saved['status'] == 'created':
                continue
            try:
                with cfg.gate:
                    # 逐项复核继承层，续传时不混用变化后的模板。
                    base = cfg.resolve(key=item['template'] or 'global')
                    if base['archived'] or base['fingerprint'] != item['base_fingerprint']:
                        raise ImportError('模板或全局默认已变化，请将未完成条目重新导入')
                    source(item['source'])
                    def initialize(session, project):
                        session.add(ConfigurationScope(key='project:' + project.uid, parent=item['template'],
                                                       values=_json(item['settings'])))
                        receipt = session.get(ProjectImport, request_id)
                        results = json.loads(receipt.results)
                        results[str(item['index'])] = dict(status='created', uid=project.uid, error='')
                        receipt.results = _json(results)
                        session.add(receipt)
                    crud.create_project(title=item['title'], source_url=item['source'], source_kind=item['source_kind'],
                                        initialize=initialize)
            except Exception as exc:
                with Session(get_engine()) as session:
                    saved = json.loads(session.get(ProjectImport, request_id).results).get(str(item['index']))
                if saved and saved['status'] == 'created':
                    _record(request_id, item['index'], {**saved, 'error': '项目已创建，但目录清单写入失败；请检查磁盘权限或空间'})
                else:
                    message = exc.message if isinstance(exc, DubForgeError) else '创建失败，请检查磁盘权限或空间后重试'
                    _record(request_id, item['index'], dict(status='error', uid='', error=message))
        return status(request_id)


def catalog():
    """给界面和其他 LLM 的契约；只提供字段元数据，不导出当前配置值或凭据。"""
    templates = cfg.list_templates()
    def metadata(field):
        return {k: v for k, v in field.items() if k in {'path', 'label', 'type', 'options', 'editable', 'min', 'max', 'help'}}
    values = cfg.defaults()
    allowed = set(values) - ENVIRONMENT_FIELDS
    fields = {f['path']: metadata(f) for section in cfg.schema().values() for f in section['fields'] if f['path'] in allowed}
    types = {bool: 'bool', int: 'int', float: 'float', str: 'str', list: 'array', dict: 'object'}
    for key in sorted(allowed - set(fields)):
        fields[key] = dict(path=key, type=types.get(type(values[key]), 'int'), label=key, nullable=values[key] is None)
    details = {
        'assembly.versions': ('成片版本', '数组整体替换，1–32项。每项必须有 id、name、dubbing、burn_subtitles、include_comments；overrides 仅允许 version_fields 中的展示参数。'),
        'publish.version_ids': ('发布分P及顺序', '字符串数组，引用成片版本 id；null 采用全部版本及原顺序。'),
        'publish.part_titles': ('分P标题', '对象：版本 id → 1–80字标题。'),
        'publish.account_id': ('发布账号', '请从模板继承，或使用 DubForge 中已有账号的标识；不是 B站 UID。'),
        'publish.season_id': ('B站合集', '实际 B站合集的整数 ID；建议通过系列模板选择，null 表示不加入合集。'),
        'publish.section_id': ('合集章节', '实际 B站合集章节的整数 ID；不是视频时间轴章节。'),
        'publish.tags': ('发布标签', '字符串数组，最多10项，每项1–20字；不要用逗号拼成一项。'),
        'publish.description': ('发布简介', '最长2000个 UTF-16 字符。是否自动填充取决于 publish.ai_copy 和流程规则。'),
        'publish.title': ('发布标题', '最长80个 UTF-16 字符；项目 title 是管理名称，与投稿标题不同。'),
        'publish.visibility': ('可见范围', 'public：公开；self：仅自己。'),
        'publish.copyright': ('投稿类型', '1原创、2转载、3其他；请按实际权利填写，不根据配音与否推断。'),
        'publish.publish_at': ('定时发布', 'Unix 时间戳（秒）或 null，运行时仍会按平台要求检查。'),
        'publish.cover_id': ('指定封面', '已上传到 DubForge 的封面标识，不是任意文件路径。'),
    }
    for key, (label, help_text) in details.items():
        fields[key].update(label=label, help=help_text)
    fields['publish.version_ids']['type'] = 'array'
    from dubforge.pipeline.assembly_versions import fields as version_fields
    active = [t for t in templates if not t['archived']]
    sample = dict(schema=SCHEMA, defaults=dict(template=active[0]['key'] if active else None, settings={}), projects=[
        dict(source='https://www.youtube.com/watch?v=BaW_jenozKc', title='旅行视频 01'),
        dict(source='https://www.youtube.com/watch?v=aqz-KE-bpKQ', title='旅行视频 02',
             settings={'comments.mode': 'auto', 'comments.limit': 50, 'comments.count': 10})])
    two_parts = dict(schema=SCHEMA, defaults=dict(template=active[0]['key'] if active else None, settings={
        'comments.mode': 'auto', 'comments.limit': 50, 'comments.count': 10,
        'assembly.versions': [
            dict(id='dub', name='中文配音版', dubbing=True, burn_subtitles=True, include_comments=True,
                 overrides={'assemble.subtitle_track': 'trans', 'comments.language_mode': 'translated'}),
            dict(id='src', name='原声双语版', dubbing=False, burn_subtitles=True, include_comments=True,
                 overrides={'assemble.subtitle_track': 'bilingual', 'comments.language_mode': 'bilingual'})],
        'publish.version_ids': ['dub', 'src'], 'publish.part_titles': {'dub': '中文配音版', 'src': '原声双语版'},
    }), projects=copy.deepcopy(sample['projects']))
    return dict(schema=SCHEMA, max_items=MAX_ITEMS, max_bytes=MAX_BYTES, templates=templates, sample=sample,
                examples={'two_parts': two_parts}, version_fields=[metadata(f) for f in version_fields()],
                fields=list(fields.values()), instructions=[
                    '只输出 JSON，不输出解释。使用 schema、defaults、projects 结构，每个项目必须包含 source。',
                    'source 是单个视频的 HTTP(S) 地址，或运行 DubForge 的电脑上的绝对视频路径；不展开播放列表。',
                    'template 使用列出的 series:标识或唯一模板名称；null 表示只跟随全局默认；省略则使用批次默认模板。',
                    'settings 使用下列点分参数名，只填写要覆盖的值；项目 settings 优先于 defaults.settings，未填写项继续实时继承。',
                    '共享的字幕、配音、成片与合集设置优先放在系列模板；不要把模板里所有值重复写入 settings，避免固定为项目覆盖。',
                    'Windows JSON 路径建议用 D:/Videos/a.mp4；反斜杠必须转义为双反斜杠。纯文本路径不需要转义。',
                    '布尔值使用 true/false；数组（包括成片版本）整体替换；null 是实际值，不表示恢复继承。',
                    '只使用用户提供的视频地址，不要杜撰视频、模板或账号标识。不要填密钥、Cookie或环境连接设置。',
                    '导入仅创建项目；即使 run.mode=publish，也必须在之后的批量运行入口确认，才会开始制作或投稿。'])
