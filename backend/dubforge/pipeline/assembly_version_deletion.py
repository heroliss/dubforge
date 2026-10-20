"""删除项目版本定义；成功产物、不可变来源与发布证据始终保留。"""
import json
import re
from contextlib import contextmanager

from sqlmodel import select

from dubforge import configuration, runtime_edit
from dubforge.domain import crud
from dubforge.domain.models import Artifact
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_versions as versions


class VersionDeletionError(DubForgeError):
    code = 'ASSEMBLY_VERSION_DELETE_INVALID'
    suggestion = '请刷新成片版本清单，核对删除范围后重试。'


NOTICE = ('仅删除本项目的版本定义，并从下次使用的发布分P清单及名称中移除。'
          '正式成品、试片、制作记录、修订/剪辑来源副本和已有发布记录全部保留；不会删除B站稿件。')
_PLAN_ID = re.compile(r'[0-9a-f]{32}')


@contextmanager
def _editing():
    """沿用发布到任务启动的锁顺序，阻止确认与入队同时改变项目结构。"""
    from dubforge.pipeline import dubbing_session
    from dubforge.publishing import service
    from dubforge.runner import runner
    with service._lock, runner._start_lock, dubbing_session.gate, configuration.gate:
        yield


def _version_id(body, *, deleting=False):
    allowed = {'version_id', 'revision', 'fingerprint', 'confirmed'} if deleting else {'version_id'}
    if (not isinstance(body, dict) or set(body) - allowed
            or not isinstance(body.get('version_id'), str)
            or not versions.ID.fullmatch(body['version_id'])):
        raise VersionDeletionError('请选择有效的成片版本。')
    if deleting and (body.get('confirmed') is not True or type(body.get('revision')) is not int
            or not isinstance(body.get('fingerprint'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', body['fingerprint'])):
        raise VersionDeletionError('请先核对删除预览，再明确确认删除此版本。')
    return body['version_id']


def _read_plan(path, root):
    """依赖摘要只读取本项目的小型方案，不加载或校验大视频。"""
    try:
        if not path.resolve().is_relative_to(root) or path.stat().st_size > 8 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _dependent_versions(root, definitions, version_id):
    dependent = set()
    for spec in definitions:
        if spec['id'] == version_id:
            continue
        for kind in ('partial', 'update', 'timeline'):
            ref = spec.get(kind)
            plan_id = ref.get('plan_id') if isinstance(ref, dict) else None
            if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
                continue
            saved = _read_plan(root / 'output' / ('.' + kind + '-plans') / (plan_id + '.json'), root)
            segments = saved.get('segments', [])
            if (saved.get('base_version_id') == version_id
                    or any(isinstance(row, dict) and row.get('media_version_id') == version_id
                           for row in (segments if isinstance(segments, list) else []))):
                dependent.add(spec['id'])
    cuts = _read_plan(root / 'output_cuts.json', root).get('versions', {})
    if isinstance(cuts, dict):
        for identifier, cut in cuts.items():
            baseline = cut.get('baseline') if isinstance(cut, dict) else None
            if isinstance(baseline, dict) and baseline.get('version_id') == version_id:
                dependent.add(identifier)
    return [spec['name'] for spec in definitions if spec['id'] in dependent and spec['id'] != version_id]


def _media_counts(project, root, version_id):
    """按逻辑文件名去重计数，保留缺失文件的登记也属于本次保留范围。"""
    record = versions.results(project.uid).get(version_id)
    formal = {'final_version_' + version_id + '.mp4'}
    previews = set()
    from dubforge.pipeline.assembly_preview import PREVIEW_NAME
    with crud.session() as session:
        artifacts = session.exec(select(Artifact).where(Artifact.project_id == project.id,
            Artifact.step_key == 'assemble')).all()
    preserved_formal = {row.name for row in artifacts if row.name in formal
                        or record is not None and row.id == record.artifact_id}
    for artifact in artifacts:
        matched = PREVIEW_NAME.fullmatch(artifact.name)
        if matched and matched.group(1) == version_id:
            previews.add(artifact.name)
    output = root / 'output'
    try:
        if output.is_dir() and not output.is_symlink() and not output.is_junction():
            for path in output.iterdir():
                if not path.is_file():
                    continue
                if path.name in formal:
                    preserved_formal.add(path.name)
                matched = PREVIEW_NAME.fullmatch(path.name)
                if matched and matched.group(1) == version_id:
                    previews.add(path.name)
    except OSError:
        # 磁盘暂时不可访问不影响保留媒体的定义删除；登记仍然可计数。
        pass
    return len(preserved_formal), len(previews)


def _plan(uid, version_id, current):
    project = crud.get_project(uid)
    if project is None:
        raise VersionDeletionError('项目不存在。', code='PROJECT_NOT_FOUND')
    definitions = versions.validate_list(current['values']['assembly.versions'])
    target = next((row for row in definitions if row['id'] == version_id), None)
    if target is None:
        raise VersionDeletionError('此版本已不在项目清单中，请刷新后重试。', code='ASSEMBLY_VERSION_DELETE_CONFLICT')
    blockers = []
    if len(definitions) <= 1:
        blockers.append('项目至少需要保留一个成片版本，请先添加另一个版本。')
    selected = current['values']['publish.version_ids']
    if selected is not None and version_id in selected and not any(ident != version_id for ident in selected):
        blockers.append('此版本是当前唯一选中的发布分P，请先在发布清单中选择其他版本，再删除。')
    targets = current['values']['assembly.target_version_ids']
    if targets is not None and version_id in targets and not any(ident != version_id for ident in targets):
        blockers.append('此版本是当前唯一项目目标成片，请先在合成页选择其他目标版本，再删除。')
    try:
        # 定义删除是结构操作；排队、后台收尾与其它步骤的任务同样需要阻挡。
        runtime_edit.assert_editable('project:' + uid)
    except DubForgeError as exc:
        blockers.append(exc.message)
    root = crud.project_dir_by_project(project).resolve()
    formal_count, preview_count = _media_counts(project, root, version_id)
    return dict(id=version_id, version_id=version_id, name=target['name'], revision=current['revision'],
        fingerprint=current['fingerprint'], can_delete=not blockers, blockers=blockers,
        dependent_versions=_dependent_versions(root, definitions, version_id),
        formal_count=formal_count, preview_count=preview_count, notice=NOTICE)


def plan(uid, body):
    version_id = _version_id(body)
    with _editing():
        return _plan(uid, version_id, configuration.resolve(uid))


def delete(uid, body):
    version_id = _version_id(body, deleting=True)
    with _editing():
        current = configuration.resolve(uid)
        try:
            # 当前任务占用应先给出可操作指引；后续仍核验旧预览和提交时门禁。
            runtime_edit.assert_editable('project:' + uid)
        except DubForgeError as exc:
            raise VersionDeletionError(exc.message, code='ASSEMBLY_VERSION_DELETE_BLOCKED',
                suggestion=exc.suggestion, detail=dict(blockers=[exc.message])) from exc
        if body['revision'] != current['revision'] or body['fingerprint'] != current['fingerprint']:
            raise VersionDeletionError('版本或项目设置已变化，请重新核对删除预览。',
                code='ASSEMBLY_VERSION_DELETE_CONFLICT')
        checked = _plan(uid, version_id, current)
        if checked['blockers']:
            raise VersionDeletionError('；'.join(checked['blockers']), code='ASSEMBLY_VERSION_DELETE_BLOCKED',
                detail=dict(blockers=checked['blockers']))
        values = current['values']
        updates = {'assembly.versions': [row for row in values['assembly.versions'] if row['id'] != version_id]}
        selected = values['publish.version_ids']
        if selected is not None and version_id in selected:
            updates['publish.version_ids'] = [identifier for identifier in selected if identifier != version_id]
        targets = values['assembly.target_version_ids']
        if targets is not None and version_id in targets:
            # 唯一目标删掉后明确退回待选择，不能擅自改选另一个历史版本。
            remaining = [identifier for identifier in targets if identifier != version_id]
            updates['assembly.target_version_ids'] = remaining or None
        target = values.get('publish.project_target')
        if isinstance(target, dict) and target.get('kind') == 'existing':
            replacements = [item for item in target['replacements'] if item['version_id'] != version_id]
            if len(replacements) != len(target['replacements']):
                updates['publish.project_target'] = {**target, 'replacements': replacements}
        if version_id in values['publish.part_titles']:
            updates['publish.part_titles'] = {key: value for key, value in values['publish.part_titles'].items()
                                             if key != version_id}
        # 三个引用在同一配置事务内写入；媒体、成功绑定和发布历史都不参与修改。
        configuration.save('project:' + uid, dict(updates=updates,
            revision=body['revision'], fingerprint=body['fingerprint']))
    return {**versions.view(uid), 'deleted_version': dict(id=version_id, name=checked['name'])}
