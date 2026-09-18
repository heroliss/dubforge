# -*- coding: utf-8 -*-
"""
领域 CRUD 层 (SQLModel 会话封装)
================================
- 项目创建时: 建 9 行 StepRun (pending) + 目录骨架 data/projects/<uid>/
- 目录骨架: media/ audio/ srt/ artifacts/<step>/ logs/
- manifest.json 双写 (人类可读快照; DB 为准)
"""
import datetime
import json
import re
import secrets
from pathlib import Path

import sqlmodel

from dubforge import config
from dubforge.domain.models import Artifact, Job, Project, StepRun
from dubforge.pipeline.registry import STEPS


def new_uid() -> str:
    """短随机 id: 6 位十六进制, 只作稳定标识 (API/URL), 不再当目录名"""
    return secrets.token_hex(3)


def _sanitize_dir_title(t: str) -> str:
    """标题 → 可作目录名的安全片段 (去 Windows 非法字符, 保中文)"""
    t = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:40] or "未命名"


def _new_dir_name(title: str) -> str:
    """语义化项目目录名: 日期_时间_标题 (天然按时间排序), 重名追加 _2/_3…"""
    base = f"{datetime.datetime.now():%Y%m%d_%H%M}_{_sanitize_dir_title(title)}"
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
    return config.projects_dir() / _dir_name_of(get_project(uid), uid)


def create_project(title: str, source_url: str = "", source_kind: str = "youtube",
                   params: dict | None = None) -> Project:
    uid = new_uid()
    dir_name = _new_dir_name(title or "未命名")
    pdir = config.projects_dir() / dir_name
    # 目录骨架 (M2 各步骤往对应子目录写产物)
    for sub in ("media", "audio", "srt", "logs", *[f"artifacts/{s.key}" for s in STEPS]):
        (pdir / sub).mkdir(parents=True, exist_ok=True)

    project = Project(uid=uid, dir_name=dir_name, title=title or uid,
                      source_url=source_url, source_kind=source_kind,
                      params=json.dumps(params or {}, ensure_ascii=False))
    with session() as s:
        s.add(project)
        s.commit()
        s.refresh(project)
        for step in STEPS:
            s.add(StepRun(project_id=project.id, step_key=step.key, status="pending"))
        s.commit()
    write_manifest(uid)
    return project


def get_project(uid: str) -> Project | None:
    with session() as s:
        return s.exec(sqlmodel.select(Project).where(Project.uid == uid)).first()


def list_projects() -> list[Project]:
    with session() as s:
        return list(s.exec(sqlmodel.select(Project).order_by(Project.created_at.desc())).all())


def delete_project(uid: str) -> bool:
    """删除项目: 取消登记 + 目录移入系统回收站。
    反悔 = 从回收站还原目录 → 设置页「从项目目录重建索引」即可整个找回;
    回收站被禁用时由系统永久删除 (删除确认弹窗里已说明)。"""
    project = get_project(uid)
    if not project:
        return False
    pid = project.id
    pdir = project_dir(uid)     # 行删除前解析 (语义化目录名要从行里查)
    # 目录先进回收站 (可还原找回; 回收站被禁用时由系统永久删除), 再删登记行;
    # 目录本就不在磁盘上 (被外部移走) → 跳过回收站, 只清理登记。
    if pdir.exists():
        from send2trash import send2trash
        send2trash(str(pdir))
    with session() as s:
        for model in (Artifact, Job, StepRun):
            for row in s.exec(sqlmodel.select(model).where(model.project_id == pid)).all():
                s.delete(row)
        s.delete(s.get(Project, pid))
        s.commit()
    return True


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


def add_artifact(project_id: int, step_key: str, kind: str, name: str,
                 abs_path: Path, rel_path: str) -> Artifact:
    import hashlib
    h = hashlib.sha256()
    with open(abs_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    art = Artifact(project_id=project_id, step_key=step_key, kind=kind, name=name,
                   rel_path=rel_path, sha256=h.hexdigest(), size=abs_path.stat().st_size)
    with session() as s:
        s.add(art)
        s.commit()
        s.refresh(art)
    return art


def delete_step_artifacts(project_id: int, step_key: str) -> None:
    """重跑前置: 删该步骤产物 (DB 行 + 磁盘文件)。

    例外: media/ 下的源视频不删 —— 获取步骤没有"过期"概念, 本地文件在/链接已
    归档就应复用 (step_download 的幂等分支依赖它); 真要删请整项目删除。
    项目目录外的引用文件 (本地导入零拷贝, rel_path 为绝对路径) 是用户原文件,
    无论如何绝不删除。
    """
    with session() as s:
        rows = list(s.exec(sqlmodel.select(Artifact).where(
            Artifact.project_id == project_id, Artifact.step_key == step_key)))
        pdir = project_dir_by_id(s, project_id)
        for a in rows:
            p = Path(a.rel_path)
            if not p.is_absolute():
                p = pdir / a.rel_path
            if p.is_relative_to(pdir) and not a.rel_path.replace("\\", "/").startswith("media/"):
                p.unlink(missing_ok=True)
            s.delete(a)
        s.commit()


def project_dir_by_id(s, project_id: int) -> Path:
    project = s.get(Project, project_id)
    # 复用调用方会话 (不再走 get_project 开新会话); 语义化目录名/uid 回退
    return config.projects_dir() / _dir_name_of(project, project.uid)


def add_job(project_id: int, step_key: str, log_path: str) -> Job:
    job = Job(project_id=project_id, step_key=step_key, log_path=log_path)
    with session() as s:
        s.add(job)
        s.commit()
        s.refresh(job)
    return job


def update_job(job: Job) -> None:
    # 只更新已存在的行: 心跳线程可能持有上个测试/旧库的 Job 引用,
    # merge 会把它当新行插进当前库 (幽灵行) —— 此处按主键存在性守卫
    with session() as s:
        if s.get(Job, job.id) is None:
            return
        s.merge(job)
        s.commit()


def save_project(project: Project) -> None:
    project.updated_at = datetime.datetime.now(datetime.timezone.utc)
    with session() as s:
        s.merge(project)
        s.commit()


def write_manifest(uid: str) -> None:
    """manifest.json 双写: 人类可读的项目自描述快照 (DB 为准, 便于排查/搬移)"""
    project = get_project(uid)
    if not project:
        return
    steps = get_steps(project.id)
    arts = latest_artifacts(project.id, None)
    by_step: dict[str, list] = {}
    for a in sorted(arts.values(), key=lambda x: (x.step_key, x.name)):
        by_step.setdefault(a.step_key, []).append(
            {"name": a.name, "file": a.rel_path, "size": a.size})
    data = {
        "uid": project.uid, "title": project.title,
        "dir_name": project.dir_name or uid,
        "source": {
            "kind": project.source_kind, "url": project.source_url,
            # 绝对路径 = 引用外部原文件零拷贝; 相对路径 = 拖入的副本, 本就是项目内文件
            "note": ("引用外部原文件 (零拷贝, 删项目不删原文件)"
                     if project.source_kind == "local"
                     and Path(project.source_url or "").is_absolute()
                     else ""),
        },
        "status": project.status, "params": json.loads(project.params or "{}"),
        "updated_at": project.updated_at.isoformat(),
        "steps": [{"key": s.step_key, "status": s.status,
                   # 指纹 = "已最新可跳过"的判断依据, 一并入 manifest (重建索引时恢复)
                   "fingerprint": s.input_fingerprint or None,
                   "error": s.error_message or None} for s in steps],
        # 每步骤产物清单: file 为相对项目目录路径; 绝对路径 = 外部引用文件
        "artifacts": by_step,
    }
    p = project_dir(uid) / "manifest.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    import os
    os.replace(tmp, p)


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
        uid = data.get("uid")
        if not uid or uid in known:
            continue
        known.add(uid)
        source = data.get("source") or {}
        with session() as s:
            proj = Project(uid=uid, dir_name=data.get("dir_name") or m.parent.name,
                           title=data.get("title") or uid,
                           source_url=source.get("url") or "",
                           source_kind=source.get("kind") or "link",
                           status=data.get("status") or "created",
                           params=json.dumps(data.get("params") or {}, ensure_ascii=False))
            s.add(proj)
            s.commit()
            s.refresh(proj)
            counts["projects"] += 1
            # 步骤行: manifest 记载的状态/错误/指纹; 缺的按注册表补 pending
            have: set[str] = set()
            for st in data.get("steps") or []:
                key = st.get("key")
                if not key or key in have:
                    continue
                have.add(key)
                status = st.get("status") or "pending"
                if status == "running":     # 崩溃残留, 与 recover() 同语义
                    status = "interrupted"
                s.add(StepRun(project_id=proj.id, step_key=key, status=status,
                              input_fingerprint=st.get("fingerprint") or "",
                              error_message=st.get("error") or ""))
                counts["steps"] += 1
            for step_def in STEPS:
                if step_def.key not in have:
                    s.add(StepRun(project_id=proj.id, step_key=step_def.key))
                    counts["steps"] += 1
            # 产物行: manifest 的 by_step 清单 (file 为相对路径; 绝对路径 = 外部引用)
            for step_key, arts in (data.get("artifacts") or {}).items():
                for a in arts:
                    s.add(Artifact(project_id=proj.id, step_key=step_key,
                                   name=a.get("name") or "",
                                   rel_path=a.get("file") or "",
                                   size=int(a.get("size") or 0)))
                    counts["artifacts"] += 1
            s.commit()
    return counts


def session():
    """统一会话工厂: expire_on_commit=False —— 对象出会话后仍可读属性 (本项目风格)"""
    return sqlmodel.Session(get_engine(), expire_on_commit=False)


def project_step_params(project) -> dict:
    """项目级步骤参数覆盖 (params JSON 的 step_params 段, 点分路径 → 值)。
    无覆盖的键跟随全局 config —— 全局默认是兜底, 项目改过才生效。"""
    try:
        return (json.loads(project.params or "{}").get("step_params") or {})
    except Exception:
        return {}
