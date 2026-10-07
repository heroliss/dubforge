"""项目发布文案预览与显式候选生成；不创建稿件、不修改配置。"""
import copy
import threading

from dubforge import configuration, video_info
from dubforge.publishing import copywriting, source_credit


_gate = threading.RLock()
_suggesting: set[str] = set()


def source_content(project, info=None):
    """与正式草稿共用视频资料基础值；只读取本地资料，不扫描成片。"""
    if info is None:
        info = video_info.view(project.uid).get('info') or {}
    summary = info.get('summary_zh') if not info.get('summary_stale') else ''
    content = dict(
        title=info.get('title_zh') or info.get('title_original') or project.title,
        description=source_credit.append_attribution(summary or '', source_credit.attribution(project, info)),
        source=source_credit.source_url(project),
    )
    return dict(content=content, content_sources={key: dict(kind='content') for key in content},
                source_published_date=source_credit.published_date(info),
                thumbnail=(info.get('content') or {}).get('thumbnail', ''))


def _content_fingerprint(project, info, content):
    # 只绑定实际文案资料；播放量、封面链接或评论刷新不让同一份文案失效。
    source, details = info.get('source') or {}, info.get('content') or {}
    evidence = dict(content=content, title_original=info.get('title_original') or project.title,
                    title_zh=info.get('title_zh'), description=info.get('description'),
                    author=source.get('uploader') or source.get('channel'),
                    published_date=source_credit.published_date(info), tags=details.get('tags'),
                    chapters=details.get('chapters'), summary_stale=bool(info.get('summary_stale')),
                    summary_zh=info.get('summary_zh') if not info.get('summary_stale') else '')
    return configuration.digest(evidence)


def preview(uid):
    from dubforge.publishing.service import DEFAULTS, _project

    project = _project(uid)
    # 与配置保存共用短锁，确保项目与父层明确键属于同一次读取。
    with configuration.gate:
        data = configuration.resolve(uid)
        parent = configuration.resolve(key=data['parent'] or 'global')
    info = video_info.view(uid).get('info') or {}
    base = source_content(project, info)
    values = {**copy.deepcopy(DEFAULTS), **copy.deepcopy(base['content'])}
    sources = {key: dict(kind='global') for key in DEFAULTS}
    origins = {key: '默认' for key in DEFAULTS}
    sources.update(copy.deepcopy(base['content_sources']))
    origins.update({key: '视频资料' for key in base['content']})
    configured = set(data['configured'])
    for key in DEFAULTS:
        path = 'publish.' + key
        if path in configured:
            # 空串、空标签或False是明确设置；不能用truthiness判定是否继承。
            values[key] = copy.deepcopy(data['values'][path])
            sources[key] = copy.deepcopy(data['sources'][path])
            origins[key] = data['origins'][path]
    return dict(project_uid=uid, revision=data['revision'], fingerprint=data['fingerprint'],
                content=base['content'], content_sources=base['content_sources'],
                source_published_date=base['source_published_date'],
                content_fingerprint=_content_fingerprint(project, info, base['content']),
                configured=data['configured'], parent_configured=parent['configured'],
                values=values, sources=sources, origins=origins)


def _assert_current(state, body):
    from dubforge.publishing.service import PublishError

    if (type(body.get('revision')) is not int or body['revision'] != state['revision']
            or body.get('fingerprint') != state['fingerprint']
            or ('content_fingerprint' in body and body['content_fingerprint'] != state['content_fingerprint'])):
        raise PublishError('项目发布设置或视频资料已变化，请刷新核对后重新生成候选', code='PUBLISH_CONFLICT')


def _evidence_fingerprint(project):
    # 最新字幕也可能在生成期间变化；这项检查只在显式生成时读取。
    data, credit, basis, warnings = copywriting.evidence(project)
    return configuration.digest(dict(data=data, credit=credit, basis=basis, warnings=warnings))


def suggest(uid, body):
    from dubforge import runtime_edit
    from dubforge.publishing.service import PublishError, _project, _copy_request

    if not isinstance(body, dict) or set(body) - {
        'revision', 'fingerprint', 'content_fingerprint', 'current', 'instruction'
    }:
        raise PublishError('项目文案请求包含不支持的字段')
    current, instruction = _copy_request(body)
    initial = preview(uid)
    _assert_current(initial, body)
    with _gate:
        if uid in _suggesting:
            raise PublishError('本项目正在生成文案，请稍候', code='PUBLISH_AI_BUSY')
        with runtime_edit.editing('project:' + uid):
            _suggesting.add(uid)
    try:
        project = _project(uid)
        evidence = _evidence_fingerprint(project)
        # 预览和证据读取之后再次核验，不用旧资料启动请求。
        _assert_current(preview(uid), body)
        result = copywriting.generate(project, current, instruction)
        if _evidence_fingerprint(_project(uid)) != evidence:
            raise PublishError('生成期间视频资料或有效字幕已变化，候选未应用，请重新生成', code='PUBLISH_CONFLICT')
        latest = preview(uid)
        _assert_current(latest, dict(revision=initial['revision'], fingerprint=initial['fingerprint'],
                                     content_fingerprint=initial['content_fingerprint']))
        return dict(**result, project_uid=uid, revision=initial['revision'],
                    fingerprint=initial['fingerprint'], content_fingerprint=initial['content_fingerprint'])
    finally:
        with _gate:
            _suggesting.discard(uid)
