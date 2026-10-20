"""项目发布目标的唯一缺省规则；历史回执永不作为自动更新原稿的依据。"""
import copy

from sqlalchemy import case, func, or_, union
from sqlmodel import select

from dubforge.publishing.models import Publication, PublicationUpdate
from dubforge.workflow_models import WorkflowRun

TARGET_PATH = 'publish.project_target'


def _valid_target(target):
    """恢复清单可能绕过写入校验；损坏目标不能被执行端误当成新稿。"""
    if not isinstance(target, dict):
        return False
    if target.get('kind') == 'new':
        return set(target) == {'kind'}
    if (target.get('kind') != 'existing'
            or set(target) != {'kind', 'publication_id', 'replace_videos', 'replacements', 'sync_fields'}
            or not isinstance(target['publication_id'], str) or not 1 <= len(target['publication_id']) <= 100
            or type(target['replace_videos']) is not bool):
        return False
    fields, replacements = target['sync_fields'], target['replacements']
    if (not isinstance(fields, list) or len(fields) > 100
            or any(not isinstance(value, str) or not 1 <= len(value) <= 100 for value in fields)
            or len(set(fields)) != len(fields)
            or not isinstance(replacements, list) or len(replacements) > 100
            or any(not isinstance(item, dict) or set(item) != {'old_cid', 'version_id'}
                or type(item['old_cid']) is not int or item['old_cid'] <= 0
                or not isinstance(item['version_id'], str) or not item['version_id']
                for item in replacements)):
        return False
    return (len({item['old_cid'] for item in replacements}) == len(replacements)
            and len({item['version_id'] for item in replacements}) == len(replacements))


def history_projects(session, uids):
    """一次批量查询发布历史；归档、软删和未知状态也不能证明从未投稿。"""
    uids = list(dict.fromkeys(uids))
    if not uids:
        return set()
    # 已确认的发布任务即使尚未回填 publication_id，也应继续原任务，不能
    # 再以首次发布缺省另起新稿。只有可读且明确为纯制作的配方可排除。
    recipe_valid = func.json_valid(WorkflowRun.recipe)
    recipe = case((recipe_valid == 1, WorkflowRun.recipe), else_='{}')
    history = union(
        select(Publication.project_uid).where(Publication.project_uid.in_(uids)),
        select(PublicationUpdate.project_uid).where(PublicationUpdate.project_uid.in_(uids)),
        select(WorkflowRun.project_uid).where(WorkflowRun.project_uid.in_(uids), or_(
            WorkflowRun.publication_id != '',
            recipe_valid != 1,
            func.json_type(recipe) != 'object',
            func.coalesce(func.json_extract(recipe, '$.mode'), '') != 'produce',
            func.length(func.json_extract(recipe, '$.update.publication_id')) > 0,
        )),
    ).subquery()
    return set(session.exec(select(history.c.project_uid)).all())


def selection(overrides, *, has_history):
    """只从本项目稀疏覆盖判断显式选择；模板和基础值不能带入稿件目标。"""
    if TARGET_PATH in overrides:
        target = copy.deepcopy(overrides[TARGET_PATH])
        if target is not None and not _valid_target(target):
            return dict(target=None, source='invalid', reason='本项目稿件目标格式无效，请重新选择',
                        has_history=bool(has_history), explicit=True)
        return dict(target=target, source='project' if target is not None else 'explicit_unset',
                    reason='已保存本项目稿件目标' if target is not None else '本项目已明确保留为未选择',
                    has_history=bool(has_history), explicit=True)
    if has_history:
        return dict(target=None, source='history_requires_choice',
                    reason='项目已有发布记录，请明确选择新建稿件或要更新的原稿',
                    has_history=True, explicit=False)
    return dict(target={'kind': 'new'}, source='first_publish',
                reason='项目尚无发布记录，首次发布默认创建新稿',
                has_history=False, explicit=False)


def metadata(selected):
    return {key: value for key, value in selected.items() if key != 'target'}
