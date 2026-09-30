"""成品磁盘状态与单文件回收；保留索引以支持从回收站原位恢复。"""
import hashlib
import re
import stat
from pathlib import Path

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.assembly_outputs import OUTPUT_VARIANTS
from dubforge.utils.recycle import recycle_file

FINAL_NAME = re.compile(r'(?:final_version_[a-z0-9][a-z0-9_-]{0,63}|final_(?:(?:' + '|'.join(OUTPUT_VARIANTS) + r')_)?\d{8}_\d{6}_\d+)\.mp4')


class OutputFileError(DubForgeError):
    code = 'OUTPUT_FILE_INVALID'
    suggestion = '请刷新成品列表后重试；仅支持管理本项目登记的正式成品。'


def _path(artifact, root):
    path = Path(artifact.rel_path)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if (not resolved.is_relative_to(root) or path.is_symlink() or path.is_junction()
            or any(p.is_symlink() or p.is_junction() for p in path.parents if p != root and p.is_relative_to(root))):
        raise OutputFileError('成品路径不安全，已拒绝操作')
    return resolved


def snapshot(artifact, root):
    """轮询仅做 stat，不反复哈希大视频；版本包含磁盘身份，防止确认框删除新替换文件。"""
    try:
        path = _path(artifact, root)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise OutputFileError('目标不是普通文件')
        version = ':'.join(map(str, (artifact.id, artifact.sha256, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)))
        return dict(exists=True, file_revision=hashlib.sha256(version.encode()).hexdigest(), disk_size=info.st_size,
                    record_revision=record_revision(artifact))
    except (OSError, ValueError, OutputFileError):
        return dict(exists=False, file_revision='', disk_size=None, record_revision=record_revision(artifact))


def record_revision(artifact):
    value = (artifact.id, artifact.rel_path, artifact.sha256, artifact.size, str(artifact.created_at))
    return hashlib.sha256(repr(value).encode()).hexdigest()


def forget_missing(uid, name, body):
    """仅移除缺失成品的登记；绝不删除文件或修改远端稿件与发布回执。"""
    from sqlmodel import select
    from dubforge.domain.models import Artifact
    from dubforge.runner import runner
    from dubforge.publishing import service as publishing
    if not FINAL_NAME.fullmatch(name) or body.get('confirmed') is not True:
        raise OutputFileError('请确认要删除记录的缺失成品')
    with publishing.output_replacement(uid, deleting=True), runner.output_file_edit(uid) as project:
        artifact = crud.latest_artifacts(project.id, ['assemble']).get(f'assemble/{name}')
        if (not artifact or type(body.get('artifact_id')) is not int or artifact.id != body['artifact_id']
                or body.get('record_revision') != record_revision(artifact)):
            raise OutputFileError('成品记录已变化，请刷新列表', code='OUTPUT_FILE_CHANGED')
        root = crud.project_dir_by_project(project).resolve()
        path = _path(artifact, root)
        if path.name != name or path.parent not in {root / 'output', root / 'artifacts' / 'assemble'}:
            raise OutputFileError('只能清理本项目成品目录中的缺失记录')
        if not root.is_dir():
            raise OutputFileError('项目目录或磁盘不可访问，请先恢复连接再清理记录')
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise OutputFileError('无法确认文件是否缺失，请检查磁盘连接和权限') from exc
        else:
            raise OutputFileError('文件已恢复或同名路径存在；仅允许删除缺失成品的记录', code='OUTPUT_FILE_RESTORED')
        with crud.session() as session:
            # 同名旧登记也清理，避免删除最新一条后旧记录重新浮现。
            rows = session.exec(select(Artifact).where(Artifact.project_id == project.id,
                Artifact.step_key == 'assemble', Artifact.name == name)).all()
            for row in rows:
                session.delete(row)
            session.commit()
        crud.write_manifest(uid)
        return dict(status='forgotten', message='已删除缺失成品的本地记录；未删除任何文件或B站稿件')


def recycle(uid, name, body):
    if not FINAL_NAME.fullmatch(name) or body.get('confirmed') is not True:
        raise OutputFileError('请确认要移入回收站的正式成品')
    from dubforge.runner import runner
    from dubforge.publishing import service as publishing
    with publishing.output_replacement(uid, deleting=True), runner.output_file_edit(uid) as project:
        artifact = crud.latest_artifacts(project.id, ['assemble']).get(f'assemble/{name}')
        if not artifact or type(body.get('artifact_id')) is not int or artifact.id != body['artifact_id']:
            raise OutputFileError('成品记录已变化，请刷新列表', code='OUTPUT_FILE_CHANGED')
        root = crud.project_dir_by_project(project).resolve()
        path = _path(artifact, root)
        if path.name != name or path.parent not in {root / 'output', root / 'artifacts' / 'assemble'}:
            raise OutputFileError('只能回收本项目成品目录中的正式视频')
        if path.exists() and not path.is_file():
            raise OutputFileError('同名路径不是普通视频文件，已拒绝回收')
        state = snapshot(artifact, root)
        if not state['exists']:
            return dict(status='missing', message='文件已不在磁盘，列表已同步；恢复到原位置后可刷新找回')
        if not body.get('file_revision') or state['file_revision'] != body['file_revision']:
            raise OutputFileError('确认期间成品文件已变化，请刷新后重新确认', code='OUTPUT_FILE_CHANGED')
        recycle_file(path)
        # 不删除 Artifact、发布草稿引用或步骤指纹。缺失会即时影响可用性，原位还原可恢复。
        return dict(status='recycled', message='成品已移入回收站；从回收站还原到原位置后，刷新即可找回')
