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
import shutil
import secrets
from pathlib import Path

import sqlmodel

from dubforge import config
from dubforge.domain.models import Artifact, Job, Project, StepRun
from dubforge.pipeline.registry import STEPS


def new_uid() -> str:
    """短目录 id: 6 位十六进制 (同秒并发碰撞概率 ~1/1600万, 单机足够)"""
    return secrets.token_hex(3)


def project_dir(uid: str) -> Path:
    return config.projects_dir() / uid


def create_project(title: str, source_url: str = "", source_kind: str = "youtube",
                   params: dict | None = None) -> Project:
    uid = new_uid()
    pdir = project_dir(uid)
    # 目录骨架 (M2 各步骤往对应子目录写产物)
    for sub in ("media", "audio", "srt", "logs", *[f"artifacts/{s.key}" for s in STEPS]):
        (pdir / sub).mkdir(parents=True, exist_ok=True)

    project = Project(uid=uid, title=title or uid, source_url=source_url,
                      source_kind=source_kind,
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
    project = get_project(uid)
    if not project:
        return False
    pid = project.id
    with session() as s:
        for model in (Artifact, Job, StepRun):
            for row in s.exec(sqlmodel.select(model).where(model.project_id == pid)).all():
                s.delete(row)
        s.delete(s.get(Project, pid))
        s.commit()
    shutil.rmtree(project_dir(uid), ignore_errors=True)
    return True


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
    """重跑前置: 删该步骤全部产物 (DB 行 + 磁盘文件)"""
    with session() as s:
        rows = list(s.exec(sqlmodel.select(Artifact).where(
            Artifact.project_id == project_id, Artifact.step_key == step_key)))
        for a in rows:
            p = project_dir_by_id(s, project_id) / a.rel_path
            p.unlink(missing_ok=True)
            s.delete(a)
        s.commit()


def project_dir_by_id(s, project_id: int) -> Path:
    project = s.get(Project, project_id)
    return project_dir(project.uid)


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
    """manifest.json 双写: 人类可读的项目状态快照 (DB 为准, 便于排查)"""
    project = get_project(uid)
    if not project:
        return
    steps = get_steps(project.id)
    data = {
        "uid": project.uid, "title": project.title,
        "source_url": project.source_url, "source_kind": project.source_kind,
        "status": project.status, "params": json.loads(project.params or "{}"),
        "updated_at": project.updated_at.isoformat(),
        "steps": [{"key": s.step_key, "status": s.status,
                   "error": s.error_message or None} for s in steps],
    }
    p = project_dir(uid) / "manifest.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    import os
    os.replace(tmp, p)


def get_engine():
    from dubforge.db import get_engine
    return get_engine()


def session():
    """统一会话工厂: expire_on_commit=False —— 对象出会话后仍可读属性 (本项目风格)"""
    return sqlmodel.Session(get_engine(), expire_on_commit=False)
