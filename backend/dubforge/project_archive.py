"""项目归档只管理工作列表，不移动文件或改变制作有效性。"""
import datetime

from sqlmodel import select

from dubforge.domain import crud
from dubforge.domain.models import Project
from dubforge.errors import DubForgeError


class ProjectArchiveError(DubForgeError):
    code = 'PROJECT_ARCHIVE_INVALID'
    suggestion = '请在项目列表中恢复项目后，再修改设置或启动任务。'


def archived_at_iso(value: datetime.datetime | None) -> str | None:
    """SQLite 返回无时区 UTC；对外显式标注，避免浏览器按本地时间解释。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).isoformat()


def assert_active(uid: str) -> Project:
    """任务或编辑入口读取当前归档状态，不能信任旧页面的项目快照。"""
    project = crud.get_project(uid)
    if project is None:
        raise ProjectArchiveError('项目不存在', code='PROJECT_NOT_FOUND')
    if project.archived_at is not None:
        raise ProjectArchiveError('项目已归档，请先恢复项目', code='PROJECT_ARCHIVED')
    return project


def set_archived(uid: str, archived: bool) -> Project:
    """与任务登记互斥，只更新管理字段；重复请求不改变归档时间。"""
    if type(archived) is not bool:
        raise ProjectArchiveError('归档状态必须为 true 或 false')
    from dubforge import runtime_edit

    with runtime_edit.editing('project:' + uid, allow_archived=True):
        with crud.session() as session:
            project = session.exec(select(Project).where(Project.uid == uid)).first()
            if project is None:
                raise ProjectArchiveError('项目不存在', code='PROJECT_NOT_FOUND')
            if (project.archived_at is not None) != archived:
                now = datetime.datetime.now(datetime.timezone.utc)
                project.archived_at = now if archived else None
                project.updated_at = now
                session.add(project)
                session.commit()
                session.refresh(project)
        # 重试也补写清单，修复上次数据库已成功而磁盘写入失败的情况。
        try:
            crud.write_manifest(uid)
        except OSError as exc:
            raise ProjectArchiveError('项目状态已保存，但归档清单写入失败，请重试',
                code='PROJECT_ARCHIVE_WRITE_FAILED', detail=str(exc)) from exc
        return project
