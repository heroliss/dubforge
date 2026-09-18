# -*- coding: utf-8 -*-
"""步骤 API: 常驻状态 + 单步运行/取消 + 日志尾读"""
import json

from fastapi import APIRouter, Query

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.registry import STEP_MAP
from dubforge.runner import _cfg_snapshot, compute_fingerprint, runner

router = APIRouter(prefix="/api/projects/{uid}/steps", tags=["steps"])


@router.get("")
def list_steps(uid: str):
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    steps = crud.get_steps(project.id)
    arts_by_step: dict[str, list] = {}
    for a in crud.latest_artifacts(project.id, None).values():
        arts_by_step.setdefault(a.step_key, []).append(
            {"name": a.name, "file": a.rel_path, "size": a.size})
    return [{
        "key": s.step_key,
        "label": STEP_MAP[s.step_key].label,
        "description": STEP_MAP[s.step_key].description,
        "status": s.status,
        "error": ({"code": s.error_code, "message": s.error_message,
                   "suggestion": s.error_suggestion} if s.error_code else None),
        "input_fingerprint": s.input_fingerprint,
        "fresh": bool(s.status == "done" and s.input_fingerprint ==
                      compute_fingerprint(project, s.step_key)),
        "retry_count": s.retry_count,
        "artifacts": arts_by_step.get(s.step_key, []),
        "cfg": _cfg_snapshot(STEP_MAP[s.step_key].cfg_keys),
    } for s in steps]


@router.post("/{key}/run")
def run_step(uid: str, key: str, body: dict | None = None):
    force = bool((body or {}).get("force", False))
    return runner.start_step(uid, key, force=force)


@router.post("/{key}/cancel")
def cancel_step(uid: str, key: str):
    return runner.cancel_step(uid, key)


@router.get("/{key}/log")
def step_log(uid: str, key: str, tail: int = Query(default=200, le=2000)):
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    log_file = crud.project_dir(uid) / "logs" / f"{key}.log"
    if not log_file.exists():
        return {"lines": [], "exists": False}
    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return {"lines": [l.rstrip("\n") for l in lines[-tail:]], "exists": True}
