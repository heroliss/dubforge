"""
领域 CRUD 层 (SQLModel 会话封装)
================================
- 项目创建时: 建 9 行 StepRun (pending) + 目录骨架 data/projects/<uid>/
- 目录骨架: media/ audio/ srt/ artifacts/<step>/ logs/
- manifest.json 双写 (人类可读快照; DB 为准)
"""
import datetime
import hashlib
import json
import re
import secrets
import threading
from pathlib import Path

import sqlmodel

from dubforge import config
from dubforge.domain.models import Artifact, Job, Project, StepRun
from dubforge.errors import DubForgeError
from dubforge.pipeline.registry import STEPS
from dubforge.utils.fs import atomic_write_bytes

_MANIFEST_LOCK = threading.RLock()
_CREATE_LOCK = threading.RLock()


class ProjectDeletionError(DubForgeError):
    code = 'PROJECT_DELETE_FAILED'
    suggestion = '请检查项目目录和回收站状态后重试；项目登记仍保留。'


def new_uid() -> str:
    """短随机 id: 6 位十六进制, 只作稳定标识 (API/URL), 不再当目录名"""
    return secrets.token_hex(3)


def _sanitize_dir_title(t: str) -> str:
    """标题 → 可作目录名的安全片段 (去 Windows 非法字符, 保中文)"""
    t = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = t[:40]
    # 截断后再次剥掉首尾空白 (含 NBSP/全角空格) —— Windows 会剥离路径组件的
    # 尾部空格, 留着它会让后续子目录 mkdir 报"找不到路径"
    return t.strip().strip(" .") or "未命名"


def _new_dir_name(title: str) -> str:
    """语义化项目目录名: 日期_时间_标题 (天然按时间排序), 重名追加 _2/_3…"""
    base = f"{datetime.datetime.now().astimezone():%Y%m%d_%H%M}_{_sanitize_dir_title(title)}"
    taken = {p.dir_name for p in list_projects() if p.dir_name}
    name, i = base, 2
    while name in taken or (config.projects_dir() / name).exists():
        name = f"{base}_{i}"
        i += 1
    return name


def _dir_name_of(project: Project | None, uid: str) -> str:
    return project.dir_name if (project and project.dir_name) else uid


def project_dir(uid: str) -> Path:
    """项目目录: 语义化名 (新项目) / uid (旧项目回退)"""
    project = get_project(uid)
    base = config.projects_dir().resolve()
    candidate = (base / _dir_name_of(project, uid)).resolve()
    return candidate if candidate.is_relative_to(base) else base / _sanitize_dir_title(uid)


def create_project(title: str, source_url: str = "", source_kind: str = "youtube",
                   params: dict | None = None, *, initialize=None, title_mode: str = "manual") -> Project:
    """目录独占创建；项目、步骤及可选初始化登记在同一个事务内。"""
    from dubforge import configuration
    # 导入先持配置门禁；所有创建入口保持相同锁序，避免与模板保存相互等待。
    with configuration.gate, _CREATE_LOCK:
        return _create_project(title, source_url, source_kind, params, initialize, title_mode)


def _create_project(title, source_url, source_kind, params, initialize, title_mode):
    uid = new_uid()
    dir_name = _new_dir_name(title or "未命名")
    root = config.projects_dir().resolve()
    pdir = (root / dir_name).resolve()
    if pdir.parent != root:
        raise DubForgeError('项目目录超出工作目录', code='PROJECT_CREATE_FAIL')
    # 目录骨架 + 登记: 任一步失败 → 清掉半成品目录并抛错,
    # 绝不留"有目录没登记"的空壳 (列表上显示不出来)
    import shutil
    owned = False
    committed = False
    try:
        root.mkdir(parents=True, exist_ok=True)
        pdir.mkdir(exist_ok=False)
        owned = True
        for sub in ("media", "audio", "srt", "logs", *[f"artifacts/{s.key}" for s in STEPS]):
            (pdir / sub).mkdir(parents=True, exist_ok=True)

        initial_params = dict(params or {})
        initial_settings = initial_params.pop('step_params', {})
        project = Project(uid=uid, dir_name=dir_name, title=title or uid,
                          title_mode="auto" if title_mode == "auto" else "manual",
                          source_url=source_url, source_kind=source_kind,
                          params=json.dumps(initial_params, ensure_ascii=False))
        with session() as s:
            s.add(project)
            s.flush()
            for step in STEPS:
                s.add(StepRun(project_id=project.id, step_key=step.key, status="pending"))
            from dubforge.configuration_models import ConfigurationScope
            from dubforge import configuration
            # 导入回调先登记选定模板和稀疏覆盖；基础值与项目回执在同一事务提交。
            with configuration.gate:
                if initialize is not None:
                    initialize(s, project)
                s.flush()
                scope = s.get(ConfigurationScope, 'project:' + uid) or ConfigurationScope(key='project:' + uid, name='本项目')
                initial_settings = {k: v for k, v in initial_settings.items() if k in configuration.defaults()
                                    and (v is not None or k == 'publish.project_target')}
                scope.values = configuration._json({**initial_settings, **json.loads(scope.values)})
                scope.baseline = configuration._json(configuration.capture_baseline(s, scope.parent))
                s.add(scope)
                s.commit()
            committed = True
            s.refresh(project)
    except Exception:
        # 仅清理本次独占创建且事务未完成的目录，不能删除原有路径。
        if owned and not committed:
            shutil.rmtree(pdir, ignore_errors=True)
        raise
    # 数据库已提交；清单写入失败也不能删除已登记的项目目录。
    write_manifest(uid)
    return project


def get_project(uid: str) -> Project | None:
    with session() as s:
        return s.exec(sqlmodel.select(Project).where(Project.uid == uid)).first()


def list_projects(*, archive: str = 'all') -> list[Project]:
    """领域查询默认保留全部项目；界面入口显式选择活动或归档列表。"""
    if archive not in {'all', 'active', 'archived'}:
        from dubforge.project_archive import ProjectArchiveError
        raise ProjectArchiveError('项目列表范围无效，请选择活动项目、已归档或全部项目',
                                  code='PROJECT_ARCHIVE_FILTER_INVALID')
    query = sqlmodel.select(Project)
    if archive == 'active':
        query = query.where(Project.archived_at.is_(None))
    elif archive == 'archived':
        query = query.where(Project.archived_at.is_not(None))
    with session() as s:
        return list(s.exec(query.order_by(Project.created_at.desc())).all())


def delete_project(uid: str) -> bool:
    # 资料刷新不占用流水线状态，但也不能与项目删除并发写入。
    from dubforge import video_info
    from dubforge.publishing import service as publishing
    with publishing.project_removal(uid), video_info.gate:
        video_info.check_idle(uid)
        return _delete_project(uid)


def _delete_project(uid: str) -> bool:
    """成功回收整个项目目录后才删除登记；无法回收绝不降级永久删除。
    从回收站还原目录后，在项目列表「找回项目」重建登记。"""
    project = get_project(uid)
    if not project:
        return False
    pid = project.id
    pdir = _project_directory_for_removal(project)
    # 先刷新可恢复清单、确认回收成功，再删登记行；任何失败都保留索引。
    # 目录本就不在磁盘上 (被外部移走) → 跳过回收站, 只清理登记。
    if pdir.exists():
        if not pdir.is_dir():
            raise ProjectDeletionError('项目路径不是目录，已取消删除', code='PROJECT_DELETE_PATH_INVALID')
        try:
            write_manifest(uid)
        except Exception as exc:
            raise ProjectDeletionError('项目恢复清单写入失败，已取消删除',
                                       code='PROJECT_DELETE_MANIFEST_FAILED') from exc
        if _project_directory_for_removal(project) != pdir or not (pdir / 'manifest.json').is_file():
            raise ProjectDeletionError('项目目录或恢复清单已变化，已取消删除',
                                       code='PROJECT_DELETE_MANIFEST_FAILED')
        from dubforge.utils.recycle import recycle_file
        recycle_file(pdir)
    with session() as s:
        for model in (Artifact, Job, StepRun):
            for row in s.exec(sqlmodel.select(model).where(model.project_id == pid)).all():
                s.delete(row)
        from dubforge.pipeline.assembly_models import AssemblyResult
        from dubforge.configuration_models import ConfigurationScope
        settings = s.get(ConfigurationScope, 'project:' + uid)
        if settings: s.delete(settings)
        for row in s.exec(sqlmodel.select(AssemblyResult).where(AssemblyResult.project_uid == uid)).all():
            s.delete(row)
        s.delete(s.get(Project, pid))
        s.commit()
    return True


def _project_directory_for_removal(project: Project) -> Path:
    """删除必须使用真实直属项目目录，禁止读取入口的越界回退或链接跳转。"""
    name = _dir_name_of(project, project.uid)
    if (not isinstance(name, str) or not name or name in {'.', '..'}
            or '/' in name or '\\' in name or Path(name).is_absolute() or Path(name).name != name):
        raise ProjectDeletionError('项目目录登记无效，已取消删除', code='PROJECT_DELETE_PATH_INVALID')
    try:
        root = config.projects_dir().resolve()
        path = root / name
        resolved = path.resolve()
        if resolved.parent != root or path.is_symlink() or path.is_junction():
            raise ProjectDeletionError('项目目录越界或是链接目录，已取消删除',
                                       code='PROJECT_DELETE_PATH_INVALID')
        return resolved
    except (OSError, RuntimeError) as exc:
        raise ProjectDeletionError('无法安全确认项目目录，已取消删除',
                                   code='PROJECT_DELETE_PATH_INVALID') from exc


def backfill_steps() -> int:
    """为老项目补齐缺失的步骤行 (步骤注册表随版本增长, 旧项目创建时还没有后面的步骤)"""
    with session() as s:
        added = 0
        for project in s.exec(sqlmodel.select(Project)).all():
            existing = {st.step_key for st in s.exec(sqlmodel.select(StepRun).where(
                StepRun.project_id == project.id)).all()}
            for step in STEPS:
                if step.key not in existing:
                    s.add(StepRun(project_id=project.id,
                                  step_key=step.key, status="pending"))
                    added += 1
        s.commit()
    return added


def get_steps(project_id: int) -> list[StepRun]:
    with session() as s:
        rows = list(s.exec(sqlmodel.select(StepRun).where(
            StepRun.project_id == project_id)))
    order = {s.key: i for i, s in enumerate(STEPS)}
    return sorted(rows, key=lambda r: order.get(r.step_key, 99))


def repair_unstarted_stale_steps() -> int:
    """修复旧版把尚未运行的下游误标过期；有执行或产物证据的一律保留。"""
    repaired_projects = set()
    count = 0
    with session() as s:
        rows = s.exec(sqlmodel.select(StepRun).where(StepRun.status == "stale")).all()
        for step in rows:
            if (step.input_fingerprint or step.started_at or step.finished_at
                    or step.retry_count or step.progress):
                continue
            has_job = s.exec(sqlmodel.select(Job.id).where(
                Job.project_id == step.project_id, Job.step_key == step.step_key)).first()
            has_artifact = s.exec(sqlmodel.select(Artifact.id).where(
                Artifact.project_id == step.project_id, Artifact.step_key == step.step_key)).first()
            if has_job is not None or has_artifact is not None:
                continue
            step.status = "pending"
            step.error_code = step.error_message = step.error_suggestion = ""
            step.eta_seconds = 0.0
            s.add(step)
            repaired_projects.add(step.project_id)
            count += 1
        s.commit()
        uids = [s.get(Project, pid).uid for pid in repaired_projects]
    for uid in uids:
        write_manifest(uid)
    return count


def get_step(project_id: int, step_key: str) -> StepRun | None:
    with session() as s:
        return s.exec(sqlmodel.select(StepRun).where(
            StepRun.project_id == project_id, StepRun.step_key == step_key)).first()


def save_step(step: StepRun) -> None:
    with session() as s:
        s.merge(step)
        s.commit()


def latest_artifacts(project_id: int, step_keys: list[str] | None = None) -> dict[str, Artifact]:
    """按 (step_key, name) 取最新产物 (重跑会先删旧行, 此处兜底去重)"""
    with session() as s:
        q = sqlmodel.select(Artifact).where(Artifact.project_id == project_id)
        # 注意 is not None: 空列表 = "无上游" (首步), 不能当 None 处理,
        # 否则本步骤自己的产物会泄漏进自己的输入指纹, 导致指纹永不匹配
        if step_keys is not None:
            q = q.where(Artifact.step_key.in_(step_keys))
        rows = list(s.exec(q))
    out: dict[str, Artifact] = {}
    for a in rows:
        key = f"{a.step_key}/{a.name}"
        if key not in out or (a.created_at and out[key].created_at and a.created_at > out[key].created_at):
            out[key] = a
    return out


def step_artifacts_exist(project_id: int, step_key: str,
                         *, verify_hash: bool = False,
                         arts: dict | None = None, pdir: Path | None = None) -> bool:
    """检查步骤登记的最新产物是否都还在磁盘上。

    状态和指纹只是索引；用户可能手动删除、移动或截断产物。没有这个检查，执行器
    会错误地把一个缺文件的 ``done`` 步骤判成 fresh，直到下游才失败。执行器跳过
    前还会按登记的 SHA-256 校验一次内容，列表接口只做廉价的文件大小检查。
    调用方已持有最新产物快照 (arts) 和项目目录 (pdir) 时传入, 免去轮询路径的重复查询。
    """
    if arts is not None:
        arts = {k: a for k, a in arts.items() if a.step_key == step_key}
    else:
        project = get_project_by_id(project_id)
        if project is None:
            return False
        arts = latest_artifacts(project_id, [step_key])
        pdir = project_dir_by_project(project)
    if step_key == 'assemble':
        from dubforge.pipeline.assembly_outputs import final_variant
        # 同组合只校验最近登记的成品；已保留的历史记录缺失，不应让新成品永远过期。
        latest = {}
        for art in arts.values():
            group = final_variant(art.name) or art.name
            if group not in latest or (art.created_at, art.id) > (latest[group].created_at, latest[group].id):
                latest[group] = art
        arts = latest
    if not arts:
        return False
    root = pdir.resolve()
    for art in arts.values():
        path = Path(art.rel_path)
        if not path.is_absolute():
            path = (pdir / path).resolve()
            if not path.is_relative_to(root):
                return False
        try:
            stat = path.stat()
        except OSError:
            return False
        if not path.is_file() or (art.size >= 0 and stat.st_size != art.size):
            return False
        if verify_hash and art.sha256:
            h = hashlib.sha256()
            try:
                with path.open("rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                return False
            if h.hexdigest() != art.sha256:
                return False
    return True


def get_project_by_id(project_id: int) -> Project | None:
    with session() as s:
        return s.get(Project, project_id)


def project_dir_by_project(project: Project) -> Path:
    base = config.projects_dir().resolve()
    candidate = (base / _dir_name_of(project, project.uid)).resolve()
    return (candidate if candidate.is_relative_to(base)
            else base / _sanitize_dir_title(project.uid))


def add_artifact(project_id: int, step_key: str, kind: str, name: str,
                 abs_path: Path, rel_path: str, *, replace_existing: bool = False,
                 job_id: int | None = None, settings_snapshot_hash: str = '') -> Artifact:
    import hashlib
    h = hashlib.sha256()
    with open(abs_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    art = Artifact(project_id=project_id, step_key=step_key, kind=kind, name=name,
                   rel_path=rel_path, sha256=h.hexdigest(), size=abs_path.stat().st_size,
                   job_id=job_id, settings_snapshot_hash=settings_snapshot_hash or '')
    with session() as s:
        if replace_existing:
            existing = s.exec(sqlmodel.select(Artifact).where(
                Artifact.project_id == project_id, Artifact.step_key == step_key,
                Artifact.name == name, Artifact.rel_path == rel_path
            ).order_by(Artifact.id.desc())).first()
            if existing:
                # 保持发布草稿的产物 ID；更新摘要后旧确认快照自动失效。
                existing.sha256, existing.size = art.sha256, art.size
                existing.kind, existing.created_at = kind, art.created_at
                existing.job_id, existing.settings_snapshot_hash = art.job_id, art.settings_snapshot_hash
                art = existing
        s.add(art)
        s.commit()
        s.refresh(art)
    return art


def delete_step_artifacts(project_id: int, step_key: str) -> None:
    """重跑前置: 删该步骤产物 (DB 行 + 磁盘文件)。

    例外: media/ 下的源视频不删 —— 下载成功校验后才原子替换；assemble/output/ 下的正式
    成片和索引都保留，新成品成功后再替换。真要删请整项目删除。
    项目目录外的引用文件 (本地导入零拷贝, rel_path 为绝对路径) 是用户原文件,
    无论如何绝不删除。
    """
    with session() as s:
        rows = list(s.exec(sqlmodel.select(Artifact).where(
            Artifact.project_id == project_id, Artifact.step_key == step_key)))
        pdir = project_dir_by_id(s, project_id)
        root = pdir.resolve()
        for a in rows:
            p = Path(a.rel_path)
            if not p.is_absolute():
                p = pdir / p
            p = p.resolve()
            # 强制重跑失败时，已有成品仍能在预览和发布中心使用。
            preserve_history = (step_key == "assemble"
                                and p.is_relative_to((root / "output").resolve()))
            if preserve_history and (a.name.startswith('final_') or re.fullmatch(
                    r'preview_version_[a-z0-9][a-z0-9_-]{0,63}_[0-9a-f]{32}\.mp4', a.name)):
                continue
            if (p.is_relative_to(root)
                    and not p.is_relative_to((root / "media").resolve())
                    and not preserve_history):
                p.unlink(missing_ok=True)
            s.delete(a)
        s.commit()


def project_dir_by_id(s, project_id: int) -> Path:
    project = s.get(Project, project_id)
    if project is None:
        raise ValueError(f"项目不存在: {project_id}")
    # 复用调用方会话 (不再走 get_project 开新会话); 语义化目录名/uid 回退
    return project_dir_by_project(project)


def add_job(project_id: int, step_key: str, log_path: str, *, task_data: dict | None = None) -> Job:
    job = Job(project_id=project_id, step_key=step_key, log_path=log_path,
              task_data=json.dumps(task_data or {}, ensure_ascii=False))
    with session() as s:
        s.add(job)
        s.commit()
        s.refresh(job)
    return job


def latest_job(project_id: int, step_key: str) -> Job | None:
    from sqlmodel import select
    with session() as s:
        return s.exec(select(Job).where(Job.project_id == project_id, Job.step_key == step_key)
                      .order_by(Job.id.desc()).limit(1)).first()


def update_job(job: Job, *, heartbeat_only: bool = False, execution_only: bool = False) -> None:
    # 只更新已存在的行: 心跳线程可能持有上个测试/旧库的 Job 引用,
    # merge 会把它当新行插进当前库 (幽灵行) —— 此处按主键存在性守卫
    with session() as s:
        current = s.get(Job, job.id)
        if current is None:
            return
        if execution_only:
            current.execution = job.execution
            s.add(current)
        elif heartbeat_only:
            # 心跳不能用旧快照覆盖执行线程刚保存的逐句结果或终态。
            current.heartbeat_at = job.heartbeat_at
            s.add(current)
        else:
            # 过程诊断有独立节流写入，旧任务快照不能把它覆盖回旧阶段。
            # 也不回写共享 Job 对象：模型回调可能正在它上面提交新阶段。
            from sqlalchemy import update
            s.exec(update(Job).where(Job.id == job.id).values(
                **job.model_dump(exclude={'id', 'execution'})))
        s.commit()


def finish_job(job: Job, step: StepRun | None = None) -> None:
    """任务与步骤终态同一事务公开，避免协调器读到半边完成。"""
    from sqlalchemy import update
    with session() as s:
        if s.get(Job, job.id) is None:
            return
        s.exec(update(Job).where(Job.id == job.id).values(
            **job.model_dump(exclude={'id', 'execution'})))
        if step is not None and s.get(StepRun, step.id) is not None:
            s.merge(step)
        s.commit()


def save_project(project: Project) -> None:
    from sqlalchemy import update
    project.updated_at = datetime.datetime.now(datetime.timezone.utc)
    with session() as s:
        current = s.get(Project, project.id) if project.id is not None else None
        if current is not None:
            # SQL 更新排除管理字段，旧任务快照与归档并发保存也不能取消归档。
            s.exec(update(Project).where(Project.id == project.id).values(
                **project.model_dump(exclude={'id', 'archived_at'})))
            s.commit()
            s.refresh(current)
            project.archived_at = current.archived_at
        else:
            s.merge(project)
            s.commit()


def update_project_status(project_id: int, status: str) -> None:
    """只更新聚合状态列。收尾路径与链写入并发时, 整行 merge 会用旧快照
    覆盖 params 里的自动续跑计划 (挂起状态被冲掉), 因此必须走列级更新。"""
    with session() as s:
        row = s.get(Project, project_id)
        if row is None or row.status == status:
            return
        row.status = status
        row.updated_at = datetime.datetime.now(datetime.timezone.utc)
        s.add(row)
        s.commit()
        uid = row.uid
    # manifest 是可搬运的诊断快照，必须在数据库聚合状态提交后再双写，
    # 否则长任务收尾会留下旧的 failed/processing 状态。
    write_manifest(uid)


def update_project_title(uid: str, title: str, *, title_mode: str = "manual") -> None:
    """只更新名称及命名方式，手工改名后资料刷新不能覆盖。"""
    from sqlalchemy import update
    with session() as s:
        # 同名手工保存也显式写入标题，避免读取之后的自动更新夺走用户提交值。
        s.exec(update(Project).where(Project.uid == uid).values(
            title=title,
            title_mode="auto" if title_mode == "auto" else "manual",
            updated_at=datetime.datetime.now(datetime.timezone.utc),
        ))
        s.commit()


def write_manifest(uid: str) -> None:
    """manifest.json 双写: 人类可读的项目自描述快照 (DB 为准, 便于排查/搬移)"""
    # 锁覆盖读取快照到落盘，防止较早取得的快照晚于新快照写入。
    with _MANIFEST_LOCK:
        _write_manifest(uid)


def _write_manifest(uid: str) -> None:
    from dubforge.project_archive import archived_at_iso
    project = get_project(uid)
    if not project:
        return
    steps = get_steps(project.id)
    arts = latest_artifacts(project.id, None)
    by_step: dict[str, list] = {}
    for a in sorted(arts.values(), key=lambda x: (x.step_key, x.name)):
        by_step.setdefault(a.step_key, []).append(
            {"name": a.name, "file": a.rel_path, "size": a.size,
             "job_id": a.job_id, "settings_snapshot_hash": a.settings_snapshot_hash or None})
    data = {
        "uid": project.uid, "title": project.title, "title_mode": project.title_mode,
        "dir_name": project.dir_name or uid,
        "source": {
            "kind": project.source_kind, "url": project.source_url,
            # 绝对路径 = 引用外部原文件零拷贝; 相对路径 = 拖入的副本, 本就是项目内文件
            "note": ("引用外部原文件 (零拷贝, 删项目不删原文件)"
                     if project.source_kind == "local"
                     and Path(project.source_url or "").is_absolute()
                     else ""),
        },
        "status": project.status, "params": project_params(project),
        "archived_at": archived_at_iso(project.archived_at),
        "updated_at": project.updated_at.isoformat(),
        "steps": [{"key": s.step_key, "status": s.status,
                   # 指纹 = "已最新可跳过"的判断依据, 一并入 manifest (重建索引时恢复)
                   "fingerprint": s.input_fingerprint or None,
                   "input_snapshot": s.input_snapshot or "{}",
                   "error": s.error_message or None} for s in steps],
        # 每步骤产物清单: file 为相对项目目录路径; 绝对路径 = 外部引用文件
        "artifacts": by_step,
    }
    from dubforge.pipeline.assembly_models import AssemblyResult
    from dubforge.configuration_models import ConfigurationScope
    with session() as s:
        from dubforge.configuration import _resolve
        from dubforge.pipeline.step_params import RUNTIME_PARAM_PATHS
        settings = _resolve(s, 'project:' + uid)
        layers = []
        for key in ['global'] + ([settings['parent']] if settings['parent'] else []):
            layer = _resolve(s, key)
            layer_values = layer['values'] if key == 'global' else layer['overrides']
            layers.append(dict(key=key, name=layer['name'], revision=layer['revision'], archived=layer['archived'],
                               configured=[path for path in layer['configured'] if path not in RUNTIME_PARAM_PATHS],
                               values={path: value for path, value in layer_values.items() if path not in RUNTIME_PARAM_PATHS}))
        data['configuration'] = dict(values=settings['overrides'], baseline=settings['baseline'], parent=settings['parent'],
                                     revision=settings['revision'], layers=layers)
        names = {a.id: a.name for a in arts.values() if a.step_key == 'assemble'}
        data['assembly_results'] = [dict(version_id=r.version_id, name=names[r.artifact_id],
            artifact_hash=r.artifact_hash, fingerprint=r.fingerprint, snapshot=json.loads(r.snapshot))
            for r in s.exec(sqlmodel.select(AssemblyResult).where(AssemblyResult.project_uid == uid)).all()
            if r.artifact_id in names]
    pdir = project_dir_by_project(project)
    if not pdir.is_dir():
        return
    p = pdir / "manifest.json"
    try:
        atomic_write_bytes(p, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"),
                           create_parent=False)
    except FileNotFoundError:
        # 项目可能在快照读取后被移走/删除；不能因清单收尾重新创建空项目目录。
        if pdir.is_dir():
            raise


def get_engine():
    from dubforge.db import get_engine
    return get_engine()


def rebuild_from_dirs() -> dict:
    """扫 projects/*/manifest.json 重建索引 —— 只补缺, 不覆盖已有行 (幂等, 可反复调)。

    目录 + manifest 是事实, DB 是可重建的索引: DB 丢失/损坏后用它恢复;
    手动拷进来的项目文件夹也会在启动/重建时被收编 (拷文件夹 = 搬项目)。
    重建不恢复: 进度百分比 (归零, 无碍) 与 Job 运行历史; running 一律判 interrupted。"""
    counts = {"projects": 0, "steps": 0, "artifacts": 0}
    root = config.projects_dir()
    if not root.is_dir():
        return counts
    with session() as s:
        known = set(s.exec(sqlmodel.select(Project.uid)).all())
    for m in sorted(root.glob("*/manifest.json")):
        try:
            data = json.loads(m.read_text(encoding="utf-8"))
        except Exception:
            continue                    # 损坏的 manifest 跳过, 不挡其余项目
        if not isinstance(data, dict):
            continue
        uid = data.get("uid")
        if not isinstance(uid, str) or not uid.strip() or uid in known:
            continue
        source = data.get("source") or {}
        if not isinstance(source, dict):
            source = {}
        raw_dir = data.get("dir_name")
        dir_name = (raw_dir if isinstance(raw_dir, str)
                    and raw_dir not in ("", ".", "..")
                    and "/" not in raw_dir and "\\" not in raw_dir
                    else m.parent.name)
        raw_params = data.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        raw_title = data.get("title")
        title = raw_title if isinstance(raw_title, str) and raw_title else uid
        raw_url = source.get("url")
        source_url = raw_url if isinstance(raw_url, str) else ""
        raw_kind = source.get("kind")
        source_kind = raw_kind if isinstance(raw_kind, str) and raw_kind else "link"
        raw_status = data.get("status")
        status = raw_status if raw_status in {"created", "processing", "done", "failed"} else "created"
        archived_at = None
        raw_archived_at = data.get('archived_at')
        if isinstance(raw_archived_at, str):
            try:
                archived_at = datetime.datetime.fromisoformat(raw_archived_at)
                if archived_at.tzinfo is None:
                    archived_at = archived_at.replace(tzinfo=datetime.timezone.utc)
                else:
                    archived_at = archived_at.astimezone(datetime.timezone.utc)
            except (ValueError, OverflowError):
                # 旧清单缺省或不合法的管理字段不影响制作产物恢复。
                archived_at = None
        with session() as s:
            proj = Project(uid=uid, dir_name=dir_name,
                           title=title,
                           title_mode="auto" if data.get("title_mode") == "auto" else "manual",
                           source_url=source_url,
                           source_kind=source_kind,
                           status=status,
                           archived_at=archived_at,
                           params=json.dumps(params, ensure_ascii=False))
            s.add(proj)
            # 直到配置、步骤及产物全部恢复才提交；坏基线不得留下半个项目。
            s.flush()
            # 步骤行: manifest 记载的状态/错误/指纹; 缺的按注册表补 pending
            have: set[str] = set()
            raw_steps = data.get("steps")
            for st in raw_steps if isinstance(raw_steps, list) else []:
                if not isinstance(st, dict):
                    continue
                key = st.get("key")
                if not isinstance(key, str) or not key or key in have:
                    continue
                have.add(key)
                status = st.get("status") or "pending"
                if status not in {"pending", "running", "queued", "done", "failed",
                                  "stale", "interrupted"}:
                    status = "pending"
                if status == "running":     # 崩溃残留, 与 recover() 同语义
                    status = "interrupted"
                elif status == "queued":
                    status = "pending"
                s.add(StepRun(project_id=proj.id, step_key=key, status=status,
                              input_fingerprint=(st.get("fingerprint")
                                                 if isinstance(st.get("fingerprint"), str)
                                                 else ""),
                              input_snapshot=(st.get("input_snapshot")
                                              if isinstance(st.get("input_snapshot"), str)
                                              else "{}"),
                              error_message=(st.get("error")
                                             if isinstance(st.get("error"), str)
                                             else "")))
                counts["steps"] += 1
            for step_def in STEPS:
                if step_def.key not in have:
                    s.add(StepRun(project_id=proj.id, step_key=step_def.key))
                    counts["steps"] += 1
            # 产物行: manifest 的 by_step 清单 (file 为相对路径; 绝对路径 = 外部引用)
            raw_artifacts = data.get("artifacts")
            for step_key, arts in (raw_artifacts.items()
                                   if isinstance(raw_artifacts, dict) else []):
                if not isinstance(step_key, str) or not isinstance(arts, list):
                    continue
                for a in arts:
                    if not isinstance(a, dict):
                        continue
                    rel_path = str(a.get("file") or "")
                    name = a.get("name")
                    if not rel_path or not isinstance(name, str) or not name:
                        continue
                    artifact_path: Path | None = Path(rel_path)
                    if not artifact_path.is_absolute():
                        artifact_path = (m.parent / artifact_path).resolve()
                        if not artifact_path.is_relative_to(m.parent.resolve()):
                            artifact_path = None  # 越界路径永不视为存在
                    # manifest 只保存了大小，重建时补算哈希；缺失文件保留登记，
                    # 但用空哈希让 fresh 检查明确失效，而不是伪造可复用产物。
                    sha256 = ""
                    try:
                        size = max(0, int(a.get("size") or 0))
                    except (TypeError, ValueError):
                        size = 0
                    if artifact_path is not None and artifact_path.is_file():
                        h = hashlib.sha256()
                        with artifact_path.open("rb") as f:
                            for chunk in iter(lambda: f.read(1 << 20), b""):
                                h.update(chunk)
                        sha256 = h.hexdigest()
                        size = artifact_path.stat().st_size
                    s.add(Artifact(project_id=proj.id, step_key=step_key,
                                    name=name,
                                    rel_path=rel_path, sha256=sha256, size=size))
                    counts["artifacts"] += 1
            # 绑定按逻辑文件名恢复，不复用旧数据库的自增编号。
            from dubforge.pipeline.assembly_models import AssemblyResult
            from dubforge.configuration_models import ConfigurationScope
            from dubforge.pipeline.assembly_versions import validate_list, validate, VersionError
            raw_settings = data.get('configuration')
            refresh_manifest = False
            if isinstance(raw_settings, dict) and isinstance(raw_settings.get('values'), dict):
                parent = raw_settings.get('parent', '')
                for layer in raw_settings.get('layers', []):
                    key = layer.get('key', '')
                    if key in ('global', parent) and not s.get(ConfigurationScope, key):
                        explicit = layer.get('configured')
                        layer_values = layer.get('values', {})
                        if key == 'global' and isinstance(explicit, list):
                            layer_values = {path: value for path, value in layer_values.items() if path in explicit}
                        elif key == 'global':
                            copy_fields = {'publish.title', 'publish.description', 'publish.tags', 'publish.dynamic'}
                            layer_values = {path: value for path, value in layer_values.items()
                                            if path not in copy_fields or value not in ('', [], None)}
                        s.add(ConfigurationScope(key=key, name=layer.get('name', ''),
                            values=json.dumps(layer_values, ensure_ascii=False), revision=layer.get('revision', 0),
                            archived=layer.get('archived') is True))
                        s.flush()
                if parent and not s.get(ConfigurationScope, parent):
                    raise DubForgeError('项目引用的系列模板不存在，请同时恢复配置数据库', code='CONFIGURATION_PARENT_MISSING')
                from dubforge import configuration
                baseline = configuration.read_baseline(raw_settings.get('baseline', {}))
                if baseline is None:
                    refresh_manifest = True
                    # 老清单的层值就是其保存依据，不能用恢复环境的新全局值改写旧项目。
                    saved = configuration.defaults()
                    origins = {key: '全局默认' for key in saved}
                    sources = {key: configuration.scope_source('global') for key in saved}
                    configured = set()
                    for layer in raw_settings.get('layers', []):
                        layer_key = layer.get('key', '')
                        if layer_key not in ('global', parent) or not isinstance(layer.get('values'), dict):
                            continue
                        values = {key: value for key, value in layer['values'].items() if key in saved}
                        saved.update(values)
                        origins.update({key: layer.get('name') or '全局默认' for key in values})
                        sources.update({key: configuration.scope_source(layer_key, layer.get('name', '')) for key in values})
                        explicit = layer.get('configured')
                        if isinstance(explicit, list):
                            configured.update(path for path in explicit if path in values)
                        elif layer_key == 'global':
                            # 旧全局层是完整值，默认空文案没有人工指定证据；项目/模板稀疏空值仍明确。
                            copy_fields = {'publish.title', 'publish.description', 'publish.tags', 'publish.dynamic'}
                            configured.update(path for path, value in values.items()
                                              if path not in copy_fields or value not in ('', [], None))
                        else:
                            configured.update(values)
                    baseline = configuration.baseline_from(dict(values=saved, origins=origins, sources=sources,
                                                               configured=sorted(configured)))
                else:
                    stored = raw_settings['baseline']
                    stored = json.loads(stored) if isinstance(stored, str) else stored
                    missing = set(baseline['values']) - set(stored['values'])
                    if missing:
                        # 与启动补齐相同：仅新字段取当前来源一次，原有值与来源不变。
                        latest = configuration.capture_baseline(s, parent)
                        for group in ('values', 'origins', 'sources'):
                            baseline[group].update({key: latest[group][key] for key in missing})
                        baseline['configured'] = sorted(set(baseline['configured']) | (set(latest['configured']) & missing))
                        refresh_manifest = True
                normalized = configuration._json({'schema': 1, **baseline})
                s.merge(ConfigurationScope(key='project:' + uid, parent=parent, baseline=normalized,
                    values=json.dumps(raw_settings['values'], ensure_ascii=False), revision=int(raw_settings.get('revision', 0))))
            else:
                # 无配置的早期清单首次恢复时固定当前起点，之后不再随全局变化。
                refresh_manifest = True
                from dubforge import configuration
                baseline = configuration.capture_baseline(s)
                s.merge(ConfigurationScope(key='project:' + uid, baseline=configuration._json(baseline)))
            s.flush()
            names = {a.name: a for a in s.exec(sqlmodel.select(Artifact).where(Artifact.project_id == proj.id, Artifact.step_key == 'assemble')).all()}
            for row in data.get('assembly_results', []) if isinstance(data.get('assembly_results'), list) else []:
                try:
                    snapshot = row['snapshot']
                    validate({k: snapshot[k] for k in ('id', 'name', 'dubbing', 'burn_subtitles', 'include_comments', 'overrides', 'partial') if k in snapshot})
                    art = names[row['name']]
                    if snapshot['id'] != row['version_id'] or not isinstance(snapshot.get('values'), dict): continue
                    s.merge(AssemblyResult(key=f'{uid}:{row["version_id"]}', project_uid=uid,
                        version_id=row['version_id'], artifact_id=art.id, artifact_hash=str(row['artifact_hash']),
                        fingerprint=str(row['fingerprint']), snapshot=json.dumps(snapshot, ensure_ascii=False)))
                except (VersionError, TypeError, ValueError, KeyError):
                    continue
            s.commit()
            known.add(uid)
            counts["projects"] += 1
            if refresh_manifest:
                # 提交成功后才原子更新清单，确保再次手动重建仍保留首次捕获值。
                write_manifest(uid)
    return counts


def session():
    """统一会话工厂: expire_on_commit=False —— 对象出会话后仍可读属性 (本项目风格)"""
    return sqlmodel.Session(get_engine(), expire_on_commit=False)


def project_params(project) -> dict:
    """读取项目参数；手动编辑或旧版本留下的坏 JSON 不应拖垮列表/恢复。"""
    try:
        value = json.loads(project.params or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def project_step_params(project) -> dict:
    """制作入口统一消费全局、系列、项目解析后的步骤值。"""
    from dubforge.configuration import step_values
    return step_values(project)
