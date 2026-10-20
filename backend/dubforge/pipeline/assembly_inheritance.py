"""就地比较与应用成片基础设置；保留项目精调，不改变上级或发布资料。"""
import copy
import re

from sqlmodel import Session

from dubforge import configuration
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError

_SUMMARY_KEYS = ('source', 'source_name', 'changed_count', 'effective_count', 'preserved_count',
                 'version_list_fixed', 'preserved_version_count', 'fingerprint')


def paths():
    """共享版本编辑器的公开渲染字段，禁止客户端扩大应用范围。"""
    from dubforge.pipeline.assembly_versions import fields
    from dubforge.pipeline.step_params import RUNTIME_PARAM_PATHS
    return ({'assembly.versions'} | {field['path'] for field in fields()}) - RUNTIME_PARAM_PATHS


def field_sources(spec, catalog):
    """版本差异不一定来自项目；全局/模板清单中的差异保留真实来源。"""
    from dubforge.pipeline.assembly_versions import fields
    result = {field['path']: copy.deepcopy(catalog['sources'].get(field['path'], {'kind': 'unknown'}))
              for field in fields()}
    for path in spec['overrides']:
        result[path] = (dict(kind='version', name=spec['name']) if not catalog['inherited'] else
                        {**copy.deepcopy(catalog['source']), 'version_name': spec['name']})
    return result


def _cut_encoding(uid, settings=None):
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.pipeline.output_cuts import snapshot, OutputCutsError
    project = crud.get_project(uid)
    if project is None:
        return False
    try:
        return bool(snapshot(Dependencies(project, settings=settings)))
    except OutputCutsError:
        # 失效的剪辑由编辑器单独解释，不妨碍用户检查或应用其它合成设置。
        return False


def _version_changes(before, after, fields, fixed, list_source, cut_encoding):
    from dubforge.pipeline import assembly_versions
    previous = {spec['id']: spec for spec in before['assembly.versions']}
    current = {spec['id']: spec for spec in after['assembly.versions']}
    order = list(dict.fromkeys([*previous, *current]))
    result = []
    dimensions = {'name': '版本名称', 'dubbing': '使用配音', 'burn_subtitles': '烧录字幕',
                  'include_comments': '叠加评论'}
    for identifier in order:
        old, new = previous.get(identifier), current.get(identifier)
        spec = new or old
        changes = []
        if old is None or new is None:
            changes.append(dict(path='assembly.versions', label='新增版本' if new else '移除版本',
                                before=copy.deepcopy(old), after=copy.deepcopy(new)))
        else:
            for path, label in dimensions.items():
                if old[path] != new[path]:
                    changes.append(dict(path=path, label=label, before=old[path], after=new[path]))
            old_values, new_values = {**before, **old['overrides']}, {**after, **new['overrides']}
            consumed = set(assembly_versions.active_values({**old, 'values': old_values, 'output_cuts': cut_encoding}))
            consumed |= set(assembly_versions.active_values({**new, 'values': new_values, 'output_cuts': cut_encoding}))
            if old['include_comments'] or new['include_comments']:
                consumed |= {path for path in fields if path.startswith('comments.')}
            for path in sorted(consumed & fields.keys()):
                if old_values[path] != new_values[path]:
                    changes.append(dict(path=path, label=fields[path]['label'],
                                        before=copy.deepcopy(old_values[path]), after=copy.deepcopy(new_values[path])))
        preserved = [dict(path=path, label=fields[path]['label'], value=copy.deepcopy(value),
                          source=dict(kind='version', name=spec['name']) if fixed else
                                 {**copy.deepcopy(list_source), 'version_name': spec['name']})
                     for path, value in sorted((new or {}).get('overrides', {}).items())
                     if old and path in old['overrides'] and old['overrides'][path] == value]
        result.append(dict(id=identifier, name=spec['name'], changes=changes, preserved_overrides=preserved))
    return result


def _prepare(session, uid, *, current=None, settings=None, cut_encoding=None):
    from dubforge.pipeline.assembly_versions import fields
    current = current or configuration._resolve(session, 'project:' + uid, settings)
    parent = current['parent'] or 'global'
    source = configuration._resolve(session, parent, settings)
    if cut_encoding is None:
        cut_encoding = _cut_encoding(uid, settings)
    selected = paths()
    original = (copy.deepcopy(current['baseline']) if current['baseline'] is not None else
                configuration.capture_baseline(session, current['parent'], settings=settings))
    baseline = copy.deepcopy(original)
    for path in selected:
        baseline['values'][path] = copy.deepcopy(source['values'][path])
        baseline['origins'][path] = source['origins'][path]
        baseline['sources'][path] = {
            **{key: copy.deepcopy(value) for key, value in source['sources'][path].items() if key in {'kind', 'name'}},
            'frozen': True,
        }
    baseline['configured'] = sorted((set(baseline['configured']) - selected) |
                                    (set(source['configured']) & selected))
    effective = copy.deepcopy(current['values'])
    for path in selected - current['overrides'].keys():
        effective[path] = copy.deepcopy(baseline['values'][path])
    # 版本内覆盖、项目混音设置与既有分P引用也必须在应用之前整体校验。
    configuration._validate({}, effective, settings=settings)
    fields_by_path = {field['path']: field for field in fields()}
    source_name = source['name'] or '全局默认'
    source_identity = dict(kind='series' if parent.startswith('series:') else 'global', name=source_name)
    changes = []
    for path in sorted(selected):
        before, after = current['defaults'][path], source['values'][path]
        if before != after:
            changes.append(dict(path=path, label='成片版本清单' if path == 'assembly.versions' else fields_by_path[path]['label'],
                before=copy.deepcopy(before), after=copy.deepcopy(after), overridden=path in current['overrides'],
                effective_before=copy.deepcopy(current['values'][path]), effective_after=copy.deepcopy(effective[path]),
                before_source=copy.deepcopy(current['parent_sources'][path]),
                after_source=copy.deepcopy(baseline['sources'][path])))
    fixed = 'assembly.versions' in current['overrides']
    fingerprint = configuration.digest(dict(project=current['fingerprint'], parent=parent,
        source={path: {key: copy.deepcopy(source[key][path]) for key in ('values', 'origins', 'sources')}
                for path in sorted(selected)}, configured=sorted(set(source['configured']) & selected),
        source_archived=source['archived'], paths=sorted(selected), cut_encoding=cut_encoding))
    plan = dict(source=source_identity, source_name=source_name, fingerprint=fingerprint, changes=changes,
        changed_count=len(changes), effective_count=sum(current['values'][item['path']] != effective[item['path']] for item in changes),
        preserved_count=len(selected & current['overrides'].keys()), version_list_fixed=fixed,
        preserved_version_count=len(current['values']['assembly.versions']) if fixed else 0,
        versions=_version_changes(current['values'], effective, fields_by_path, fixed,
                                  baseline['sources']['assembly.versions'], cut_encoding))
    return plan, baseline, current, source


def plan(uid):
    """只读比较当前绑定来源，不切换模板、不探测视频、不生成媒体。"""
    with configuration.gate, Session(get_engine()) as session:
        return _prepare(session, uid)[0]


def summary(uid, *, current=None, settings=None, cut_encoding=None):
    """列表轮询只需计数；候选组合有冲突时仍可编辑当前有效版本。"""
    try:
        with configuration.gate, Session(get_engine()) as session:
            data = _prepare(session, uid, current=current, settings=settings, cut_encoding=cut_encoding)[0]
        return {key: data[key] for key in _SUMMARY_KEYS}
    except DubForgeError as exc:
        return dict(source={'kind': 'unknown', 'name': ''}, source_name='', changed_count=0, effective_count=0,
                    preserved_count=0, version_list_fixed=False, preserved_version_count=0,
                    fingerprint='', error=exc.message, error_code=exc.code)


def apply(uid, body):
    """短锁内重新预检并原子替换基础片段；项目覆盖与非合成设置逐字保留。"""
    if (not isinstance(body, dict) or set(body) != {'fingerprint', 'confirmed'} or body.get('confirmed') is not True
            or not isinstance(body.get('fingerprint'), str) or not re.fullmatch(r'[0-9a-f]{64}', body['fingerprint'])):
        raise configuration.ConfigurationError('请先比较最新合成设置，再明确确认应用')
    from dubforge.pipeline import dubbing_session
    from dubforge.runner import runner
    from dubforge.project_archive import assert_active
    with runner._start_lock, dubbing_session.gate, configuration.gate, Session(get_engine()) as session:
        assert_active(uid)
        proposed, baseline, current, source = _prepare(session, uid)
        if body['fingerprint'] != proposed['fingerprint']:
            raise configuration.ConfigurationError('项目或来源设置已变化，请重新比较后确认', code='CONFIGURATION_CONFLICT')
        if source['archived']:
            raise configuration.ConfigurationError('当前系列模板已归档，请先恢复模板再应用设置')
        configuration._check_idle(session, current['key'], paths())
        row = configuration._row(session, current['key'])
        if row.archived:
            raise configuration.ConfigurationError('项目设置已归档，请先恢复后再应用')
        row.baseline, row.revision = configuration._json(baseline), row.revision + 1
        session.add(row)
        session.commit()
    crud.write_manifest(uid)
    from dubforge.pipeline.assembly_versions import view
    return view(uid)
