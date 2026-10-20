"""自动制作到原BV更新的交接；预检只读，实际副作用复用原稿更新状态机。"""
import copy
import json

from sqlmodel import Session

from dubforge import configuration
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.domain.models import Artifact
from dubforge.errors import DubForgeError
from dubforge.publishing import service as pub, studio, updates, transport
from dubforge.workflow_plan import WorkflowError


def _versions(uid, source, remote, definitions):
    """只使用上传回执、版本登记和哈希的证据链，不猜标题/顺序/时长。"""
    from dubforge.pipeline import assembly_versions
    records = assembly_versions.results(uid)
    known = updates._proven_parts(source)
    allowed = {v['id'] for v in definitions or []}
    result = {}
    for part in remote['videos']:
        old = known.get(part['cid'], ({}, {}))[0]
        candidates = set()
        if old.get('version_id') in allowed:
            candidates.add(old['version_id'])
        # 兼容上传快照尚未保存版本ID的历史记录，要求原产物ID和哈希同时匹配。
        for version_id, row in records.items():
            if (version_id in allowed and old.get('artifact_id') == row.artifact_id
                    and old.get('sha256') and old['sha256'] == row.artifact_hash):
                candidates.add(version_id)
        if not candidates:
            # 只有本次选中的唯一局部子版本可凭精确基片身份接替原分P。
            # 普通版本已有匹配优先；多个修订共享基片时仍要求人工指定。
            revisions = {version['id'] for version in definitions or []
                         if version.get('partial') and version.get('partial_plan')
                         and old.get('artifact_id') == version['partial_plan'].get('base_artifact_id')
                         and old.get('sha256') and old['sha256'] == version['partial_plan'].get('base_sha256')}
            if len(revisions) == 1:
                candidates.update(revisions)
        result[part['cid']] = next(iter(candidates)) if len(candidates) == 1 else ''
    return result


_LABELS = {
    'title': '标题', 'description': '简介', 'tags': '标签', 'dynamic': '动态',
    'declaration_id': '创作声明', 'tid': '原分区', 'human_type2': '分区', 'copyright': '版权类型',
    'source': '转载来源', 'cover': '封面', 'chapters': '视频章节', 'collection': '合集',
    'part_titles': '分P名称', 'part_order': '分P顺序', 'add_parts': '追加分P',
    'selected_comments': '精选评论', 'close_comments': '关闭评论', 'close_danmaku': '关闭弹幕',
    'visibility': '可见范围', 'remove_parts': '删除分P', 'watermark': '水印',
    'publish_at': '定时发布时间', 'topic': '活动与话题', 'subtitles': '平台字幕',
}
_SCOPE_FIELDS = [*_LABELS]
_SUPPORTED = studio.EDIT_FIELDS | {'chapters', 'collection', 'part_titles', 'part_order', 'add_parts'}


def _definitions(uid, spec, version_specs):
    # 纯资料更新仅阅读版本定义，不解析字幕、音频依赖或合成参数。
    if version_specs is None:
        from dubforge.pipeline import assembly_versions
        rows = assembly_versions.catalog(uid)['versions']
    else:
        rows = version_specs
    if spec.get('target_driven'):
        intent = spec.get('update') or {}
        selected = [part['version_id'] for part in intent.get('replacements', [])]
        if 'add_parts' in intent.get('sync_fields', []):
            selected = [*(spec.get('version_ids') or []), *selected]
        elif 'part_order' in intent.get('sync_fields', []):
            selected = [*[key for key in spec.get('version_ids') or [] if key in selected], *selected]
    else:
        selected = spec.get('version_ids')
        if not selected:
            return rows
    # 版本目录和制作目标都有各自顺序；追加及显式排序必须遵循发布清单。
    definitions = {row['id']: row for row in rows}
    return [definitions[key] for key in dict.fromkeys(selected) if key in definitions]


def _display(key, value, display, metadata):
    if key == 'human_type2' and display.get('category_title'):
        return display['category_title']
    if key in {'tid', 'human_type2'}:
        def category(rows, prefix=''):
            for row in rows:
                name = row.get('name') or row.get('title') or ''
                path = ' / '.join(filter(None, (prefix, name)))
                if row.get('id') == value and path:
                    return path
                found = category(row.get('children') or [], path)
                if found:
                    return found
            return ''
        return category((metadata or {}).get('categories' if key == 'tid' else 'human_categories') or []) or (
            f'分区（ID {value}）' if value else '未设置')
    if key == 'collection':
        return ' / '.join(filter(None, (display.get('season_title'), display.get('section_title')))) or (
            f'合集（ID {value}）' if value else '无合集')
    if key == 'copyright':
        return {1: '自制', 2: '转载', 3: '指定创作声明'}.get(value, '未设置')
    if key == 'declaration_id':
        choice = next((row for row in (metadata or {}).get('declarations', []) if row['value'] == value), {})
        return choice.get('label') or ('未设置' if value is None else f'创作声明（ID {value}）')
    if isinstance(value, bool):
        return '开启' if value else '关闭'
    if isinstance(value, list):
        return '、'.join(str(item) for item in value) or '无'
    if isinstance(value, dict):
        return '、'.join(str(item) for item in value.values()) or '保留当前设置'
    return str(value) if value not in (None, '') else '未设置'


def prepare(uid, spec, version_specs=None, *, online=True, previous=None):
    intent = spec.get('update') or {}
    key = intent.get('publication_id')
    if not isinstance(key, str) or not key:
        raise WorkflowError('请选择要更新的原B站稿件', code='WORKFLOW_UPDATE_TARGET')
    source = updates._source(key)
    if source['project_uid'] != uid:
        raise WorkflowError('原发布记录不属于当前项目', code='WORKFLOW_UPDATE_TARGET')
    if online:
        remote = updates._remote(source)
        remote_parts = [{k: copy.deepcopy(v) for k, v in part.items() if k != 'filename'}
                        for part in remote['videos']]
        binding = dict(source_revision=source['revision'], remote_version=remote['version'],
            remote_parts=remote_parts, remote_settings=copy.deepcopy(remote['settings']),
            aid=remote['aid'], edit_supported=remote.get('edit_supported'),
            edit_limitation=remote.get('edit_limitation', ''))
        if previous and (previous.get('publication_id') != key
                or any(previous.get(k) != binding[k] for k in ('source_revision', 'remote_version', 'remote_parts'))):
            raise WorkflowError('原稿在上次确认后已变化，请重新选择并核对更新目标', code='PUBLISH_REMOTE_CHANGED')
    else:
        if not previous or previous.get('publication_id') != key:
            raise WorkflowError('缺少已核对的原稿快照，请重新预检', code='WORKFLOW_UPDATE_TARGET')
        if previous.get('source_revision') != source['revision']:
            raise WorkflowError('原发布记录已变化，请重新预检', code='WORKFLOW_CONFLICT')
        binding = {k: copy.deepcopy(previous.get(k)) for k in ('source_revision', 'remote_version',
            'remote_parts', 'remote_settings', 'aid', 'edit_supported', 'edit_limitation')}
        remote = dict(videos=binding['remote_parts'])
    from dubforge.publishing import project_copy
    current = project_copy.preview(uid)
    data = configuration.resolve(uid)['values']
    chapter_enabled = spec.get('chapters_enabled', data['publish.chapters_enabled'])
    season, section = data.get('publish.season_id'), data.get('publish.section_id')
    chosen = intent.get('sync_fields')
    if chosen is None:
        chosen = [field for field in _SCOPE_FIELDS if field in studio.EDIT_FIELDS
                  and 'publish.' + field in current['configured']]
        if chapter_enabled:
            chosen.append('chapters')
        if season:
            chosen.append('collection')
    if (not isinstance(chosen, list) or any(not isinstance(field, str) for field in chosen)
            or len(set(chosen)) != len(chosen)):
        raise WorkflowError('请选择不重复的稿件更新项目', code='WORKFLOW_UPDATE_MAPPING')
    selected_fields = set(chosen)
    replace_videos = intent.get('replace_videos', True)
    if type(replace_videos) is not bool:
        raise WorkflowError('请明确选择是否替换原视频', code='WORKFLOW_UPDATE_MAPPING')
    definitions_list = _definitions(uid, spec, version_specs)
    definitions = {version['id']: version for version in definitions_list}
    requested = intent.get('replacements', [])
    if (not isinstance(requested, list) or len(requested) > 100
            or any(not isinstance(part, dict) or set(part) != {'old_cid', 'version_id'}
                or type(part['old_cid']) is not int or not isinstance(part['version_id'], str) for part in requested)
            or len({part['old_cid'] for part in requested}) != len(requested)
            or len({part['version_id'] for part in requested}) != len(requested)):
        raise WorkflowError('每个原分P及成片版本只能绑定一次', code='WORKFLOW_UPDATE_MAPPING')
    if requested:
        mapping = {part['old_cid']: part['version_id'] for part in requested}
        if set(mapping) - {part['cid'] for part in remote['videos']} or set(mapping.values()) - set(definitions):
            raise WorkflowError('原分P或所选版本已变化，请重新核对对应关系', code='WORKFLOW_UPDATE_MAPPING')
    else:
        # 恢复使用已确认映射，避免新产物登记替换旧证据后丢失身份。
        mapping = ({part['old_cid']: part['version_id'] for part in previous['parts']
                    if part['old_cid'] > 0 and part.get('version_id') in definitions} if previous
                   else {} if spec.get('target_driven') else _versions(uid, source, remote, definitions_list))
    parts = [dict(old_cid=part['cid'], title=part.get('title', ''),
        version_id=mapping.get(part['cid'], ''),
        version_name=definitions.get(mapping.get(part['cid']), {}).get('name', ''),
        replaced=bool(replace_videos and mapping.get(part['cid']) in definitions), added=False)
        for part in remote['videos']]
    issues, warnings = [], ['保留原BV；仅更新本次勾选的内容，未选择的分P和稿件设置保持原样。']
    if any(version.get('partial') for version in definitions_list):
        warnings.append('局部修订使用独立成片：只有选定范围采用当前内容，其余范围保留基准成片。')
    if replace_videos:
        warnings += ['从本次选择的制作步骤开始；有效转写、翻译和已精调配音复用，按当前设置重建过期成片。',
            '视频更新需要重新审核；上传成功不代表通过。章节使用实际成片时间轴和最终分P编号。']
    for field in chosen:
        if field not in _SUPPORTED:
            reason = pub.STUDIO_FIELD_CAPABILITIES.get(field, {}).get('reason') or '当前没有可靠编辑契约，请在B站官网处理'
            issues.append(f'{_LABELS.get(field, field)}：{reason}')
    mapped = [part['version_id'] for part in parts if part['version_id']]
    unused = [version for version in definitions_list if version['id'] not in mapped]
    if len(mapped) != len(set(mapped)) or (replace_videos and unused and 'add_parts' not in selected_fields):
        issues.append('请为每个所选成片版本指定唯一的原分P；不能按标题或顺序猜测替换目标')
    if replace_videos and not definitions:
        issues.append('请选择要替换的成片版本')
    if 'add_parts' in selected_fields:
        for index, version in enumerate(unused, 1):
            title = spec.get('part_titles', {}).get(version['id']) or version['name']
            parts.append(dict(old_cid=-index, title=title, version_id=version['id'],
                version_name=version['name'], replaced=False, added=True))
    if len(parts) > 100:
        issues.append('更新后最多允许管理100个分P，请减少新增版本')
    part_titles = {}
    if 'part_titles' in selected_fields:
        for part in parts:
            if part['old_cid'] > 0 and part['version_id'] in definitions:
                title = spec.get('part_titles', {}).get(part['version_id']) or definitions[part['version_id']]['name']
                if (not isinstance(title, str) or not title.strip() or len(title.encode('utf-16-le')) // 2 > 80
                        or any(ord(char) < 32 for char in title)):
                    issues.append('分P名称须为1至80个字符，且不能包含控制字符')
                else:
                    part_titles[str(part['old_cid'])] = title.strip()
                    part['title'] = title.strip()
    order = None
    if 'part_order' in selected_fields:
        by_version = {part['version_id']: part['old_cid'] for part in parts if part['version_id']}
        ordered = [by_version[version['id']] for version in definitions_list if version['id'] in by_version]
        order = [*ordered, *[part['old_cid'] for part in parts if part['old_cid'] not in ordered]]
    if binding['edit_supported'] is not True and (replace_videos or selected_fields & studio.EDIT_FIELDS
            or selected_fields & {'part_titles', 'part_order', 'add_parts'}):
        issues.append(binding['edit_limitation'] or '该稿件暂不支持自动编辑，请到B站官网核对')
    if not transport.available():
        issues.append('发布组件尚未安装')
    # 明确选择才能使用资料默认值，初次默认只覆盖已明确保存的字段；保留同值供交付做无副作用对账。
    values = {field: copy.deepcopy(current['values'][field]) for field in chosen
              if field in studio.EDIT_FIELDS and field != 'cover' and field in current['values']}
    try:
        values = studio.validate_edit_values(values, before=binding['remote_settings'],
                                            account=source['account_id'] if online else '')
    except DubForgeError as exc:
        issues.append(exc.message)
    # 默认范围由项目开关推导；显式勾选章节是本次授权，不被项目默认关闭悄悄抵消。
    chapters = dict(enabled='chapters' in selected_fields,
        translate=spec.get('chapters_translate', data['publish.chapters_translate']),
        instruction=spec.get('chapters_instruction', data['publish.chapters_instruction']),
        permanent=spec.get('chapters_permanent', data['publish.chapters_permanent']), overwrite_existing=True)
    content = None
    items = []
    if chapters['enabled']:
        content = project_copy.source_content(crud.get_project(uid))
        items = content['chapters'] or (source['snapshot'].get('chapter_options') or {}).get('items', [])
    chapters = {**pub._chapter_options(chapters, items), 'overwrite_existing': True}
    collection = dict(enabled='collection' in selected_fields, season_id=season, section_id=section)
    if collection['enabled'] and bool(season) != bool(section):
        issues.append('当前项目已选择合集，请补选合集小节')
    cover = None
    if 'cover' in selected_fields:
        cover_source = spec.get('cover_source', data.get('publish.cover_source', 'platform'))
        if cover_source == 'custom':
            cover_id = spec.get('cover_id', data.get('publish.cover_id', ''))
            try:
                cover = dict(source='custom', cover_id=cover_id, sha256=pub._file_hash(pub._cover_path(cover_id)),
                             label='项目指定封面')
            except DubForgeError as exc:
                issues.append(exc.message)
        else:
            content = content or project_copy.source_content(crud.get_project(uid))
            thumbnail = content.get('thumbnail') or ''
            cover = dict(source='platform', source_fingerprint=pub._hash(dict(thumbnail=thumbnail)),
                         label='原视频封面（交付时获取）')
            if not thumbnail:
                issues.append('原视频没有可用封面，请先在项目发布设置中指定封面')
    if not replace_videos and not selected_fields:
        issues.append('本次没有选择更新内容；如只制作请改选“仅制作”')
    ineffective = []
    if 'add_parts' in selected_fields and not unused:
        ineffective.append('没有可追加的新成片版本；所选版本已属于原稿件，不会重复追加')
    if 'part_titles' in selected_fields and not part_titles:
        ineffective.append('没有可可靠对应的原分P名称；请先核对成片版本与原分P关系')
    has_action = bool(values or chapters['enabled'] or collection['enabled'] or cover
                      or part_titles or order is not None or any(part['replaced'] or part['added'] for part in parts))
    (warnings if has_action else issues).extend(ineffective)
    chapter_scope = ''
    if chapters['enabled'] and not any(part['replaced'] or part['added'] for part in parts):
        # 现存视频章节只能使用可证明的上传时间轴；无法对应的P必须明确保留，不能冒充同步完成。
        proven = updates._proven_parts(source)
        missing = [part.get('title') or f'第{index + 1}个分P'
                   for index, part in enumerate(remote['videos']) if part['cid'] not in proven]
        count = len(remote['videos']) - len(missing)
        chapter_scope = f'可同步{count}个已核验分P'
        if missing:
            chapter_scope += f'；保留{len(missing)}个分P现有章节：' + '、'.join(missing)
            other_action = bool(values or collection['enabled'] or cover or part_titles or order is not None)
            message = '章节时间轴无法核验，将保留这些分P的现有章节：' + '、'.join(missing)
            if not count and not other_action:
                issues.append('没有可核验时间轴的分P可同步章节；请先核对原稿对应关系或选择重新制作视频')
            else:
                warnings.append(message)
    metadata = None
    if online:
        from dubforge.publishing import chapter_dispatch
        if key in chapter_dispatch._running:
            issues.append('原稿章节正在处理，请完成后再更新原稿件')
        with Session(get_engine()) as session:
            try:
                updates.assert_available(session, account=source['account_id'], bvid=source['bvid'], uid=uid)
                updates._assert_remote_free(session, source['account_id'], source['bvid'])
            except DubForgeError as exc:
                issues.append(exc.message)
        metadata = pub.metadata(source['account_id'])
        if collection['enabled'] and season:
            found = next((row for row in metadata.get('seasons', []) if row['id'] == season), {})
            if not any(row['id'] == section for row in found.get('sections', [])):
                issues.append('目标合集或小节在原稿账号中不可用，请调整项目发布设置')
    if chapters['enabled'] and not chapters['items']:
        warnings.append('原视频没有可用章节，本次将跳过章节设置。')
    rejected_warning = '请先修正原稿驳回涉及的内容；仅重新制作不能保证通过平台审核。'
    rejected = (remote.get('state') in {-2, -16} if online
                else rejected_warning in previous.get('warnings', []))
    if rejected:
        warnings.append(rejected_warning)
    settings = {**binding['remote_settings'], **values,
                **({'season_id': season, 'section_id': section} if collection['enabled'] else {})}
    display = (pub.settings_display(source['account_id'], settings, metadata=metadata, account_name=source['account_name'])
               if online else copy.deepcopy(previous.get('settings_display', {})))
    scope_options = []
    for field in _SCOPE_FIELDS:
        capability = pub.STUDIO_FIELD_CAPABILITIES.get(field, {})
        value = (chapters['enabled'] if field == 'chapters' else season if field == 'collection'
                 else cover['label'] if field == 'cover' and cover else part_titles if field == 'part_titles'
                 else order if field == 'part_order' else len(unused) if field == 'add_parts'
                 else current['values'].get(field))
        supported = field in _SUPPORTED
        reason = (capability.get('reason') if not supported else '') or (
            '当前不自动删除分P，未选制作的版本会保留' if field == 'remove_parts' else
            '' if supported else '当前没有可靠编辑契约，请在B站官网处理')
        value_display = _display(field, value, display, metadata)
        if field == 'chapters' and chapter_scope:
            reason = value_display = chapter_scope
        if not online:
            saved = next((option for option in previous.get('scope_options', []) if option['key'] == field), {})
            value_display = saved.get('value_display', value_display)
        scope_options.append(dict(key=field, label=_LABELS[field], supported=supported,
            selected=field in selected_fields, reason=reason, value=copy.deepcopy(value), value_display=value_display))
    return dict(publication_id=key, bvid=source['bvid'], title=source['title'],
        account_id=source['account_id'], account_name=source['account_name'], parts=parts,
        **binding, values=values, settings=settings, chapters=chapters, collection=collection,
        replace_videos=replace_videos, sync_fields=chosen, scope_options=scope_options,
        part_titles=part_titles, part_order=order, cover=cover, blockers=issues, warnings=warnings,
        settings_display=display)

def get_task(run):
    key = run['plan'].get('publication_update_id') or 'workflow-update-' + run['id']
    try:
        return updates.view(key)
    except pub.PublishError as exc:
        if exc.code != 'PUBLISH_UPDATE_NOT_FOUND':
            raise
    return None


def deliver(run, event=None, progress=None):
    """先绑定这次成功的产物，再建立固定更新任务；恢复时只消费原任务。"""
    from dubforge import workflows
    target = run['plan']['update_plan']
    task = get_task(run)
    if not task:
        if event is not None and event.is_set():
            raise WorkflowError('流程已暂停，尚未提交原稿更新', code='WORKFLOW_CANCELLED')
        actual = {v['version']['id']: v for v in run['plan'].get('assembled_versions', [])}
        replacements, additions = [], []
        for part in target['parts']:
            if (not part['version_id'] or not (part.get('added') or part.get('replaced',
                    target.get('replace_videos', True) and part['old_cid'] > 0))):
                continue
            bound = actual.get(part['version_id'])
            if not bound or not bound.get('artifact_id'):
                raise WorkflowError('本次合成的版本与原分P无法对应，未上传', code='WORKFLOW_OUTPUT_STALE')
            with Session(get_engine()) as session:
                artifact = session.get(Artifact, bound['artifact_id'])
                project = crud.get_project(run['project_uid'])
                if (not artifact or artifact.project_id != project.id
                        or artifact.sha256 != bound['artifact_sha256']):
                    raise WorkflowError('本次成片的登记已变化，未上传', code='WORKFLOW_OUTPUT_STALE')
            if part.get('added'):
                additions.append(dict(artifact_id=bound['artifact_id'], title=part['title']))
            else:
                replacements.append(dict(old_cid=part['old_cid'], artifact_id=bound['artifact_id']))
        if progress:
            progress.phase('publish.update.prepare', '核对本次更新范围与原稿资料')
        body = dict(replacements=replacements, values=target['values'],
            chapters={k: target['chapters'][k] for k in ('enabled', 'translate', 'instruction',
                'permanent', 'overwrite_existing')}, collection=target['collection'])
        if 'delivery_mode' in run.get('recipe', {}):
            body['delivery_mode'] = run['recipe']['delivery_mode']
        if additions:
            body['additions'] = additions
        if target.get('part_titles'):
            body['part_titles'] = target['part_titles']
        if target.get('part_order') is not None:
            body['part_order'] = target['part_order']
        cover = target.get('cover')
        if cover:
            if cover['source'] == 'custom':
                cover_id = cover['cover_id']
                if pub._file_hash(pub._cover_path(cover_id)) != cover['sha256']:
                    raise WorkflowError('确认后的项目封面已变化，请重新预检，未上传', code='WORKFLOW_OUTPUT_STALE')
            else:
                from dubforge.publishing import project_copy
                content = project_copy.source_content(crud.get_project(run['project_uid']))
                if pub._hash(dict(thumbnail=content.get('thumbnail') or '')) != cover['source_fingerprint']:
                    raise WorkflowError('确认后的原视频封面资料已变化，请重新预检，未上传', code='PUBLISH_REMOTE_CHANGED')
                cover_id = pub.platform_cover(run['project_uid'])['cover_id']
            body['cover_id'] = cover_id
        task = updates.plan(target['publication_id'], body,
            workflow_id=run['id'], expected={**{k: target[k] for k in
                ('source_revision', 'remote_version', 'remote_parts')},
                **({'chapter_items': target['chapters']['items']} if target['chapters']['enabled'] else {})})
    run['plan']['publication_update_id'] = task['id']
    workflows._update(run['id'], plan=run['plan'])
    if run.get('recipe', {}).get('delivery_mode') == 'draft':
        return task
    if task['status'] == 'unknown':
        raise WorkflowError('原稿修改结果待核对，请先核对，不能重复发送', code='PUBLISH_WRITE_UNKNOWN')
    if task['status'] in {'prepared', 'paused', 'failed'}:
        if event is not None and event.is_set():
            raise WorkflowError('流程已暂停，已准备的原稿更新保留', code='WORKFLOW_CANCELLED')
        task = updates.action(task['id'], dict(action='start' if task['status'] == 'prepared' else 'retry',
            confirmed=True, fingerprint=task['fingerprint']), workflow_id=run['id'])
    return task
