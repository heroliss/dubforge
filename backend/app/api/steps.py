# -*- coding: utf-8 -*-
"""步骤 API: 常驻状态 + 单步运行/取消 + 日志尾读"""
import json
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, Response

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
        "progress": round(s.progress or 0, 1),
        "cancelling": runner.is_cancelling(project.id, s.step_key),
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


# 产物预览: 按后缀给 MIME; 文本类前端以 text() 读取, 音视频直接喂给播放器
_ART_MIME = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
    ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".json": "text/plain; charset=utf-8", ".srt": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


@router.get("/{key}/artifacts/{name}/raw")
def artifact_raw(uid: str, key: str, name: str):
    """按登记产物提供原文/原文件 (预览用); 只允许本步骤已登记的名字, 不接受路径"""
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    if "/" in name or "\\" in name or ".." in name:
        raise DubForgeError("非法产物名", code="ARTIFACT_NAME_INVALID")
    art = crud.latest_artifacts(project.id, [key]).get(f"{key}/{name}")
    if art is None:
        raise DubForgeError(f"产物不存在: {key}/{name}", code="ARTIFACT_NOT_FOUND")
    p = Path(art.rel_path)
    if not p.is_absolute():
        p = crud.project_dir(uid) / p
    if not p.exists():
        raise DubForgeError("产物文件已不在磁盘", code="ARTIFACT_MISSING")
    mime = _ART_MIME.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(p, media_type=mime,
                        headers={"Cache-Control": "no-store"})
