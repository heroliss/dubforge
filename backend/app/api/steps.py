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

# 规划中尚未实现的配置键 (如 pyannote 说话人分离) 不在步骤页展示, 免得看着像可调参数;
# 用户 config.yaml 里残留的同名键同样被过滤。快照按段嵌套 ("separate": {...}), 段内外都要滤
_CFG_DISPLAY_HIDE = {"separate.speaker_diarization", "separate.hf_token"}


def _display_cfg(snapshot: dict) -> dict:
    out = {}
    for k, v in snapshot.items():
        if k in _CFG_DISPLAY_HIDE:
            continue
        if isinstance(v, dict):
            v = {sk: sv for sk, sv in v.items()
                 if f"{k}.{sk}" not in _CFG_DISPLAY_HIDE}
        out[k] = v
    return out


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
        "cfg": _display_cfg(_cfg_snapshot(STEP_MAP[s.step_key].cfg_keys)),
        "progress": round(s.progress or 0, 1),
        "cancelling": runner.is_cancelling(project.id, s.step_key),
        "pausing": runner.is_paused(project.id, s.step_key),
        "inputs": [{"step": up, "name": name, "desc": desc}
                   for up, name, desc in STEP_MAP[s.step_key].inputs],
        "outputs": [{"name": name, "desc": desc}
                    for name, desc in STEP_MAP[s.step_key].outputs],
    } for s in steps]


@router.post("/{key}/run")
def run_step(uid: str, key: str, body: dict | None = None):
    force = bool((body or {}).get("force", False))
    return runner.start_step(uid, key, force=force)


@router.post("/{key}/cancel")
def cancel_step(uid: str, key: str):
    return runner.cancel_step(uid, key)


@router.post("/{key}/pause")
def pause_step(uid: str, key: str, body: dict | None = None):
    """暂停/继续 (协作式: 当前单元结束后停; body.pause=false 为继续)"""
    return runner.pause_step(uid, key, pause=bool((body or {}).get("pause", True)))


@router.post("/{key}/force_stop")
def force_stop_step(uid: str, key: str):
    """强制结束: 子进程立即终止, 步骤立刻按已取消呈现 (进程内计算后台收尾后丢弃)"""
    return runner.force_stop(uid, key)


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


def _artifact_file(uid: str, key: str, name: str) -> Path:
    """登记产物 → 磁盘路径 (raw/peaks 共用的白名单校验: 只允许本步骤已登记的名字)"""
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
    return p


@router.post("/{key}/reveal")
def reveal_step(uid: str, key: str):
    """在系统资源管理器中打开本步骤的主产物目录 (不存在则先创建)"""
    import os
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    if key not in STEP_MAP:
        raise DubForgeError(f"未知步骤: {key}", code="STEP_UNKNOWN")
    pdir = crud.project_dir(uid)
    if not pdir.exists():
        raise DubForgeError("项目目录不在磁盘上 (可能被移动或删除)",
                            code="PROJECT_DIR_MISSING",
                            suggestion="从回收站还原目录后重试。")
    d = pdir / STEP_MAP[key].dir
    d.mkdir(parents=True, exist_ok=True)
    from dubforge.utils.reveal import reveal_in_explorer
    reveal_in_explorer(str(d))
    return {"ok": True, "dir": str(d)}


@router.post("/{key}/artifacts/{name}/reveal")
def reveal_artifact(uid: str, key: str, name: str):
    """在资源管理器中定位单个产物文件 (上游产物由其所属步骤的登记表校验)"""
    p = _artifact_file(uid, key, name)
    from dubforge.utils.reveal import reveal_in_explorer
    reveal_in_explorer(str(p))
    return {"ok": True, "path": str(p)}


@router.get("/{key}/artifacts/{name}/raw")
def artifact_raw(uid: str, key: str, name: str):
    """按登记产物提供原文/原文件 (预览用); 只允许本步骤已登记的名字, 不接受路径"""
    p = _artifact_file(uid, key, name)
    mime = _ART_MIME.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(p, media_type=mime,
                        headers={"Cache-Control": "no-store"})


@router.get("/{key}/artifacts/{name}/peaks")
def artifact_peaks(uid: str, key: str, name: str, buckets: int = 900):
    """音频波形峰值序列 (前端画波形): ffmpeg 降采样为 4kHz 单声道后按桶取最大幅值。
    结果缓存在 artifacts/<key>/peaks_<name>.json (重跑清产物时一并失效)。"""
    import json as _json
    p = _artifact_file(uid, key, name)
    if p.suffix.lower() != ".wav":
        raise DubForgeError("只支持 wav 音频生成波形", code="PEAKS_UNSUPPORTED")
    cache = crud.project_dir(uid) / "artifacts" / key / f"peaks_{name}.json"
    if cache.exists():
        try:
            data = _json.loads(cache.read_text(encoding="utf-8"))
            if data.get("src_size") == p.stat().st_size:
                data.pop("src_size", None)
                return data
        except Exception:
            pass

    import subprocess
    r = subprocess.run(["ffmpeg", "-i", str(p), "-vn", "-ac", "1", "-ar", "4000",
                        "-f", "s16le", "-"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise DubForgeError("波形计算失败 (ffmpeg)", code="PEAKS_FAIL",
                            detail=r.stderr[-500:])
    import numpy as np
    amp = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    duration = round(len(amp) / 4000.0, 3)
    n = max(1, min(buckets, len(amp)))
    pad = (-len(amp)) % n
    if pad:
        amp = np.concatenate([amp, np.zeros(pad, dtype=np.float32)])
    peaks = np.abs(amp).reshape(n, -1).max(axis=1).round(3).tolist()
    data = {"points": peaks, "duration": duration}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps({"src_size": p.stat().st_size, **data}),
                       encoding="utf-8")
        import os
        os.replace(tmp, cache)
    except Exception:
        pass                          # 缓存写失败不影响返回
    return data
