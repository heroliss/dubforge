# -*- coding: utf-8 -*-
"""精修工作台 API (M5 前端接入; 现有 VideoLingo-Workspace 工作台的移植接口)
- state: 逐句列表 (缓存/采纳/语速) + 视频/背景音可用性
- synthesize: 单句合成 (复用 step_tts.synth_line, 共享句级缓存)
- confirm: 采纳状态 (output/workbench_confirm.json; 正式合成自动跳过已确认句)
- 音视频流: 支持 Range (浏览器拖动)
"""
import datetime
import json
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool

from dubforge import config
from dubforge.domain import crud
from dubforge.pipeline.step_tts import synth_line

router = APIRouter(prefix="/api/projects/{uid}/workbench", tags=["workbench"])

_LOCK_GUARD = {}


def _confirm_file(pdir) -> Path:
    return pdir / "workbench_confirm.json"


def _load_confirm(pdir) -> dict:
    try:
        return json.loads(_confirm_file(pdir).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_confirm(pdir, d: dict) -> None:
    _confirm_file(pdir).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


@router.get("")
def state(uid: str):
    from dubforge.backends import tts_indextts25, tts_gptsovits
    project = crud.get_project(uid)
    if not project:
        from dubforge.errors import DubForgeError
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    pdir = crud.project_dir(uid)

    task_path = pdir / "artifacts" / "dubtask" / "dubtask.json"
    lines = []
    if task_path.exists():
        task = json.loads(task_path.read_text(encoding="utf-8"))
        confirms = _load_confirm(pdir)
        for l in task["lines"]:
            wav = pdir / "artifacts" / "tts" / f"{l['id']}.wav"
            lines.append({
                **{k: l[k] for k in ("id", "text", "start", "end",
                                     "duration", "est_dur", "speed_flag")},
                "wav": {"exists": wav.exists(),
                        "url": f"/api/projects/{uid}/workbench/audio/line/{l['id']}",
                        "seconds": _wav_dur(wav)},
                "confirmed": str(l["id"]) in confirms,
            })

    # 服务状态 (轻探测, 带缓存)
    cfg_tts = config.get("tts.backend")
    online = False
    if cfg_tts == "indextts25":
        online = tts_indextts25.is_online(config.get("tts.indextts25.url"))
    elif cfg_tts == "gptsovits":
        online = tts_gptsovits.is_online(config.get("tts.gptsovits.url"))

    video = next((p for p in sorted((pdir / "media").glob("video.*"))
                  if p.exists()), None)
    return {
        "lines": lines,
        "video": {"found": video is not None,
                  "url": f"/api/projects/{uid}/workbench/video" if video else None},
        "bg": {"found": (pdir / "audio" / "background.wav").exists(),
               "url": f"/api/projects/{uid}/workbench/audio/bg"},
        "service": {"backend": cfg_tts, "online": online},
        "counts": {
            "total": len(lines),
            "synthesized": sum(1 for l in lines if l["wav"]["exists"]),
            "confirmed": sum(1 for l in lines if l["confirmed"]),
        },
    }


def _wav_dur(p):
    try:
        import wave
        with wave.open(str(p), "rb") as w:
            return round(w.getnframes() / w.getframerate(), 2)
    except Exception:
        return None


@router.post("/lines/{line_id}/synthesize")
async def synthesize(uid: str, line_id: int, body: dict | None = None):
    project = crud.get_project(uid)
    if not project:
        from dubforge.errors import DubForgeError
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    pdir = crud.project_dir(uid)
    task_path = pdir / "artifacts" / "dubtask" / "dubtask.json"
    if not task_path.exists():
        from dubforge.errors import DubForgeError
        raise DubForgeError("还没有配音任务表 (先完成 dubtask 步骤)",
                            code="DUBTASK_MISSING")
    lines = json.loads(task_path.read_text(encoding="utf-8"))["lines"]
    line = next((l for l in lines if l["id"] == line_id), None)
    if line is None:
        from dubforge.errors import DubForgeError
        raise DubForgeError(f"句号不存在: {line_id}", code="LINE_NOT_FOUND")

    force = bool((body or {}).get("force", False))
    r = await run_in_threadpool(synth_line, project, line, pdir, force)
    import wave
    dur = _wav_dur(r["file"])
    return {"ok": True, "status": r["status"], "duration": dur,
            "url": f"/api/projects/{uid}/workbench/audio/line/{line_id}"}


@router.post("/lines/{line_id}/confirm")
async def confirm(uid: str, line_id: int, body: dict):
    project = crud.get_project(uid)
    pdir = crud.project_dir(uid)
    d = _load_confirm(pdir)
    if body.get("confirmed", True):
        d[str(line_id)] = {"ts": datetime.datetime.now().isoformat(timespec="seconds")}
    else:
        d.pop(str(line_id), None)
    _save_confirm(pdir, d)
    return {"ok": True, "confirmed": str(line_id) in d, "count": len(d)}


@router.get("/audio/line/{line_id}")
def line_audio(uid: str, line_id: int):
    p = crud.project_dir(uid) / "artifacts" / "tts" / f"{line_id}.wav"
    if not p.exists():
        return Response("not found", status_code=404)
    return FileResponse(p, media_type="audio/wav",
                        headers={"Cache-Control": "no-store"})


@router.get("/audio/bg")
def bg_audio(uid: str):
    p = crud.project_dir(uid) / "audio" / "background.wav"
    if not p.exists():
        return Response("not found", status_code=404)
    return FileResponse(p, media_type="audio/wav",
                        headers={"Cache-Control": "no-store"})


@router.get("/video")
def video(uid: str):
    media = crud.project_dir(uid) / "media"
    v = next((p for p in sorted(media.glob("video.*")) if p.exists()), None)
    if v is None:
        return Response("no video", status_code=404)
    return FileResponse(v, media_type="video/mp4",
                        headers={"Cache-Control": "no-store",
                                 "Accept-Ranges": "bytes"})
