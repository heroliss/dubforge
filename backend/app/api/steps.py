"""步骤 API: 常驻状态 + 单步运行/取消 + 日志尾读"""
from pathlib import Path
from datetime import timezone

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.registry import PIPELINE_KEYS, STEP_MAP
from dubforge.runner import _cfg_snapshot, compute_fingerprint, runner
from dubforge.utils.fs import atomic_replace
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/projects/{uid}/steps", tags=["steps"])

# 规划中尚未实现的配置键 (如 pyannote 说话人分离) 不在步骤页展示, 免得看着像可调参数;
# 用户 config.yaml 里残留的同名键同样被过滤。快照按段嵌套 ("separate": {...}), 段内外都要滤
_CFG_DISPLAY_HIDE = {"separate.speaker_diarization", "separate.hf_token"}
_CFG_SENSITIVE = {"api_key", "token", "password", "secret"}


def _redact_cfg(value, key: str = ""):
    """步骤状态可被前端轮询，递归遮蔽配置中的密钥/令牌。"""
    if key.lower() in _CFG_SENSITIVE or any(part in key.lower()
                                            for part in ("api_key", "password", "token")):
        return "***"
    if isinstance(value, dict):
        return {k: _redact_cfg(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_cfg(v, key) for v in value]
    return value


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
def list_steps(uid: str, response: Response):
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    response.headers["Cache-Control"] = "no-store"
    from dubforge.pipeline.dependencies import Dependencies, selected_steps
    deps = Dependencies(project)
    selected = selected_steps(project)
    steps = [s for s in deps.steps.values() if s.step_key in PIPELINE_KEYS]
    arts_by_step: dict[str, list] = {}
    from dubforge.pipeline.output_files import snapshot as output_snapshot
    from dubforge.pipeline.assembly_versions import artifact_metadata
    version_meta = artifact_metadata(uid)
    for a in deps.arts.values():
        arts_by_step.setdefault(a.step_key, []).append(
            # sha256 是前端预览的版本标记: 同名产物被原子替换后，
            # 页面必须能区分新旧内容，不能只依赖文件名。
            {"id": a.id, "name": a.name, "file": a.rel_path, "path": str(deps.path(a.step_key, a.name)), "size": a.size,
             **(output_snapshot(a, deps.root) if a.step_key == 'assemble' else {}),
             **version_meta.get(a.id, {}),
             "sha256": a.sha256, "created_at": a.created_at.replace(tzinfo=timezone.utc).isoformat() if a.created_at else None})
    inputs = {s.step_key: deps.input_view(s.step_key) for s in steps}
    result_by_key = {s.step_key: {
        "key": s.step_key,
        "label": STEP_MAP[s.step_key].label,
        "description": STEP_MAP[s.step_key].description,
        "status": deps.status(s.step_key),
        "error": ({"code": s.error_code, "message": s.error_message,
                   "suggestion": s.error_suggestion} if s.error_code and deps.status(s.step_key) != "done" else None),
        "input_fingerprint": s.input_fingerprint,
        "fresh": deps.display_fresh(s.step_key),
        "default_fresh": deps.fresh(s.step_key),
        **({'freshness_label': deps.assembly_state()['label'], 'freshness_reason': deps.assembly_state()['reason']}
           if s.step_key == 'assemble' else {}),
        "selected": s.step_key in selected,
        "missing_inputs": [i for i in inputs[s.step_key] if i['state'] in {'missing', 'deleted'}],
        "unready_inputs": [i for i in inputs[s.step_key] if not i['ready']],
        "affected_steps": deps.downstream(s.step_key),
        "retry_count": s.retry_count,
        "artifacts": arts_by_step.get(s.step_key, []),
        "cfg": _redact_cfg(_display_cfg(_cfg_snapshot(STEP_MAP[s.step_key].cfg_keys, deps.overrides, settings=deps.settings))),
        "progress": 100.0 if deps.status(s.step_key) == "done" else round(s.progress or 0, 1),
        **(runner.progress_eta(project.id, s.step_key) if s.status == "running"
           else {"eta_seconds": 0.0, "eta_scope": "step"}),
        "cancelling": runner.is_cancelling(project.id, s.step_key),
        "stop_reason": runner.stop_reason(project.id, s.step_key),
        "pausing": runner.is_paused(project.id, s.step_key),
        "pause_state": runner.pause_state(project.id, s.step_key),
        # 控制窗口携带任务代次，避免弹窗打开后旧任务结束/新任务开始时，
        # 旧弹窗误操作新任务。
        "job_id": runner.current_job_id(project.id, s.step_key),
        "inputs": inputs[s.step_key],
        "outputs": [{"name": name, "desc": desc}
                    for name, desc in STEP_MAP[s.step_key].outputs
                    if (s.step_key != "subtitles" or name == "subtitles.json" or name in deps.subtitle_outputs())
                    and (s.step_key != "preprocess" or deps.cfg("separate.enabled", True)
                         or name in {"raw.wav", "separate.json"})],
    } for s in steps}
    if 'tts' in result_by_key:
        from dubforge.pipeline.dubbing_task import view
        result_by_key['tts']['synthesis_task'] = view(project.id)
    if 'assemble' in result_by_key:
        import json
        from dubforge.pipeline.assembly_outputs import default_description
        job = crud.latest_job(project.id, 'assemble')
        try:
            task = json.loads(job.task_data or '{}') if job else {}
            if not isinstance(task, dict):
                task = {'scope': 'unknown'}
        except (ValueError, TypeError):
            task = {'scope': 'unknown'}  # 旧任务元数据损坏不应阻断整个步骤页。
        result_by_key['assemble']['assembly_task'] = {
            'scope': task.get('scope', 'default'), 'variants': task.get('variants', []),
            'version_names': [v['name'] for v in task.get('version_specs', [])],
            'start': task.get('start'), 'end': task.get('end'),
            'comments_count': max([len((task.get('comment_overlay') or {}).get('items', [])),
                *[len((v.get('comment_overlay') or {}).get('items', [])) for v in task.get('version_specs', [])]]),
            'active': bool(job and job.status == 'running' and deps.steps['assemble'].status in {'running', 'queued'}),
        }
        result_by_key['assemble']['default_output_label'] = default_description(deps.cfg)
    return [result_by_key[key] for key in PIPELINE_KEYS if key in result_by_key]


@router.get('/download/video-info')
def video_info_view(uid: str, response: Response):
    from dubforge import video_info
    response.headers['Cache-Control'] = 'no-store'
    return video_info.view(uid)


@router.get('/download/video-info/cover')
def platform_cover_image(uid: str):
    from dubforge import platform_cover
    return FileResponse(platform_cover.get(uid), headers={'Cache-Control': 'private, no-cache',
                                                         'X-Content-Type-Options': 'nosniff'})


@router.post('/download/video-info/{action}')
def update_video_info(uid: str, action: str, body: dict | None = None):
    from dubforge import video_info
    info = video_info.update(uid, action, comment_limit=(body or {}).get('comment_limit', 50))
    return {'info': info}


@router.post("/assemble/preview")
def preview_assemble(uid: str, body: dict):
    """按视频原始时间轴试合成一小段；只生成 preview_*.mp4，不替代正式成片。"""
    return runner.start_assemble_preview(uid, body.get("start"), body.get("end"),
        include_comments=body.get('include_comments', False), overlay_revision=body.get('overlay_revision'), output=body.get('output'))

@router.post("/assemble/outputs")
def outputs_assemble(uid: str, body: dict):
    """按组合清单多版本合成 (配音/原声 × 烧录字幕)；不删除旧成片。"""
    return runner.start_assemble_outputs(uid, (body or {}).get("variants"),
        include_comments=body.get('include_comments', False), overlay_revision=body.get('overlay_revision'), outputs=body.get('outputs'))


@router.post("/assemble/output_plan")
def plan_assemble_outputs(uid: str, body: dict):
    """只读核对勾选方案的实际依赖；不启动、不自动增加其他方案。"""
    from dubforge.pipeline.assembly_outputs import resolve_outputs, dimensions, VARIANT_LABELS, current_output
    from dubforge.pipeline.dependencies import Dependencies
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    variants = resolve_outputs(body.get('variants'), outputs=body.get('outputs'), include_comments=body.get('include_comments', False))
    from dubforge import comment_studio
    plan = comment_studio.for_task(uid, any(dimensions(v)['include_comments'] for v in variants), body.get('overlay_revision'), variants)
    deps = Dependencies(project, assemble_variants=variants)
    return {'variants': variants, 'labels': [VARIANT_LABELS[v] for v in variants],
            'replacements': [v for v in variants if current_output(crud.project_dir(uid) / 'output', v)],
            'comments_count': len(plan['items']) if plan else 0,
            'overlay_revision': comment_studio._hash(plan) if plan else None,
            'blockers': deps.unready('assemble')}

@router.post("/{key}/run")
def run_step(uid: str, key: str, body: dict | None = None):
    force = bool((body or {}).get("force", False))
    # resume_chain: 随本次运行把挂起的自动续跑计划重新激活 (成功后继续剩余步骤)。
    return runner.start_step(uid, key, force=force,
                             resume_chain=bool((body or {}).get("resume_chain", False)))


@router.post("/{key}/cancel")
def cancel_step(uid: str, key: str, body: dict | None = None):
    return runner.cancel_step(uid, key, expected_job_id=(body or {}).get("job_id"))


@router.post("/{key}/pause")
def pause_step(uid: str, key: str, body: dict | None = None):
    """暂停/继续 (协作式: 当前单元结束后停; body.pause=false 为继续)"""
    return runner.pause_step(
        uid, key, pause=bool((body or {}).get("pause", True)),
        expected_job_id=(body or {}).get("job_id"))


@router.post("/{key}/force_stop")
def force_stop_step(uid: str, key: str, body: dict | None = None):
    """强制结束: 子进程立即终止, 步骤立刻按已取消呈现 (进程内计算后台收尾后丢弃)"""
    return runner.force_stop(uid, key, expected_job_id=(body or {}).get("job_id"))


@router.get("/{key}/log")
def step_log(uid: str, key: str, response: Response,
             tail: int = Query(default=200, ge=1, le=2000)):
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    response.headers["Cache-Control"] = "no-store"
    if key not in STEP_MAP:
        raise DubForgeError(f"未知步骤: {key}", code="STEP_UNKNOWN")
    from dubforge.utils.logs import read_tail
    log_file = crud.project_dir_by_project(project) / "logs" / f"{key}.log"
    try:
        lines, truncated = read_tail(log_file, tail)
    except FileNotFoundError:  # 项目清理/日志轮转可能发生在读取之前。
        return {"lines": [], "exists": False, "truncated": False}
    return {"lines": lines, "exists": True, "truncated": truncated}


@router.get("/{key}/llm_trace")
def step_llm_trace(uid: str, key: str, response: Response,
                   tail: int = Query(default=1000, ge=1, le=10000),
                   after: int | None = Query(default=None, ge=0),
                   trace_id: str = ""):
    """返回当前步骤最近一轮 LLM 对话事件 (提示词/增量输出, 不含密钥)。"""
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    response.headers["Cache-Control"] = "no-store"
    if key not in STEP_MAP:
        raise DubForgeError(f"未知步骤: {key}", code="STEP_UNKNOWN")
    trace_file = crud.project_dir(uid) / "logs" / f"{key}.llm.jsonl"
    from dubforge.utils.logs import read_llm_trace
    result = read_llm_trace(trace_file, tail=tail, after=after, trace_id=trace_id)
    if not result.get('exists') or result.get('retry'):
        return result
    step = crud.get_step(project.id, key)
    return {**result,
            "active": bool(step and step.status in ("queued", "running")),
            "job_id": runner.current_job_id(project.id, key)}


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
        root = crud.project_dir(uid).resolve()
        p = (root / p).resolve()
        if not p.is_relative_to(root):
            raise DubForgeError("产物路径非法", code="ARTIFACT_PATH_INVALID")
    if not p.is_file():
        raise DubForgeError("产物文件已不在磁盘", code="ARTIFACT_MISSING")
    return p


@router.post("/{key}/reveal")
def reveal_step(uid: str, key: str):
    """在系统资源管理器中打开本步骤的主产物目录 (不存在则先创建)"""
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


@router.post('/assemble/artifacts/{name}/recycle')
def recycle_output(uid: str, name: str, body: dict, request: Request, response: Response):
    from urllib.parse import urlparse
    origin = request.headers.get('origin')
    if (request.headers.get('sec-fetch-site') == 'cross-site'
            or origin and urlparse(origin).hostname not in {request.url.hostname, 'localhost', '127.0.0.1', '::1'}):
        raise DubForgeError('请在 DubForge 页面内回收成品，不接受跨站请求', code='OUTPUT_ORIGIN')
    response.headers['Cache-Control'] = 'no-store'
    from dubforge.pipeline.output_files import recycle
    return recycle(uid, name, body)


@router.post('/assemble/artifacts/{name}/forget')
def forget_output(uid: str, name: str, body: dict, request: Request, response: Response):
    from urllib.parse import urlparse
    origin = request.headers.get('origin')
    if (request.headers.get('sec-fetch-site') == 'cross-site'
            or origin and urlparse(origin).hostname not in {request.url.hostname, 'localhost', '127.0.0.1', '::1'}):
        raise DubForgeError('请在 DubForge 页面内清理成品记录，不接受跨站请求', code='OUTPUT_ORIGIN')
    response.headers['Cache-Control'] = 'no-store'
    from dubforge.pipeline.output_files import forget_missing
    return forget_missing(uid, name, body)


@router.get("/{key}/artifacts/{name}/raw")
def artifact_raw(uid: str, key: str, name: str):
    """按登记产物提供原文/原文件 (预览用); 只允许本步骤已登记的名字, 不接受路径"""
    p = _artifact_file(uid, key, name)
    mime = _ART_MIME.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(p, media_type=mime,
                        headers={"Cache-Control": "no-store"})


def _decode_4k(p: Path):
    """ffmpeg 解码为 4kHz 单声道 s16 原始样本 (波形计算的公共输入)"""
    import subprocess
    try:
        r = subprocess.run(["ffmpeg", "-i", str(p), "-vn", "-ac", "1", "-ar", "4000",
                            "-f", "s16le", "-"], capture_output=True,
                           timeout=120, check=False)
    except FileNotFoundError as e:
        raise DubForgeError("波形计算失败: 未找到 ffmpeg", code="PEAKS_FAIL") from e
    except subprocess.TimeoutExpired as e:
        raise DubForgeError("波形计算超时", code="PEAKS_FAIL") from e
    if r.returncode != 0 or not r.stdout:
        raise DubForgeError("波形计算失败 (ffmpeg)", code="PEAKS_FAIL",
                            detail=r.stderr[-500:])
    import numpy as np
    return np.frombuffer(r.stdout, dtype=np.int16)


def _bucketize(amp, buckets: int) -> dict:
    """s16 样本 → 桶峰值序列 (先求和后取绝对值, 混音语义正确) + 时长"""
    import numpy as np
    f = amp.astype(np.float32) / 32768.0
    duration = round(len(f) / 4000.0, 3)
    n = max(1, min(buckets, len(f)))
    pad = (-len(f)) % n
    if pad:
        f = np.concatenate([f, np.zeros(pad, dtype=np.float32)])
    peaks = np.abs(f).reshape(n, -1).max(axis=1).round(3).tolist()
    return {"points": peaks, "duration": duration}


@router.get("/{key}/artifacts/{name}/peaks")
def artifact_peaks(uid: str, key: str, name: str,
                   response: Response,
                   buckets: int = Query(default=900, ge=1, le=10000)):
    """音频波形峰值序列 (前端画波形)。结果缓存在 artifacts/<key>/peaks_<name>.json
    (重跑清产物时一并失效)。"""
    import json as _json
    response.headers["Cache-Control"] = "no-store"
    p = _artifact_file(uid, key, name)
    if p.suffix.lower() != ".wav":
        raise DubForgeError("只支持 wav 音频生成波形", code="PEAKS_UNSUPPORTED")
    cache = crud.project_dir(uid) / "artifacts" / key / f"peaks_{name}.json"
    if _cache_valid(cache, [p], buckets):
        data = _json.loads(cache.read_text(encoding="utf-8"))
        data.pop("src_sizes", None)
        data.pop("src_mtimes", None)
        data.pop("buckets", None)
        return data
    data = _bucketize(_decode_4k(p), buckets)
    _write_cache(cache, [p], data, buckets)
    return data


@router.get("/{key}/artifacts/{name}/peaks_mixed")
def artifact_peaks_mixed(uid: str, key: str, name: str, other: str,
                         response: Response,
                         buckets: int = Query(default=900, ge=1, le=10000)):
    """两轨真实混音 (逐样本相加) 的波形峰值。
    峰值包络直接相加是"两轨峰值恰好同瞬同号"的最坏情形上界 —— 实际错开时
    会高估、反相时该抵消, 混音预览必须先求和再取峰值。结果按双方文件大小缓存。"""
    import json as _json
    response.headers["Cache-Control"] = "no-store"
    p1 = _artifact_file(uid, key, name)
    p2 = _artifact_file(uid, key, other)
    if p1.suffix.lower() != ".wav" or p2.suffix.lower() != ".wav":
        raise DubForgeError("只支持 wav 音频生成波形", code="PEAKS_UNSUPPORTED")
    cache = (crud.project_dir(uid) / "artifacts" / key
             / f"peaks_mixed_{name}_{other}.json")
    if _cache_valid(cache, [p1, p2], buckets):
        data = _json.loads(cache.read_text(encoding="utf-8"))
        data.pop("src_sizes", None)
        data.pop("src_mtimes", None)
        data.pop("buckets", None)
        return data
    # 两轨解码相互独立, 并行跑 (输出与串行逐字节一致, 纯提速)
    from concurrent.futures import ThreadPoolExecutor

    import numpy as np
    with ThreadPoolExecutor(max_workers=2) as ex:
        fa, fb = ex.submit(_decode_4k, p1), ex.submit(_decode_4k, p2)
        a, b = fa.result(), fb.result()
    n = min(len(a), len(b))
    mixed = np.clip(a[:n].astype(np.int32) + b[:n].astype(np.int32),
                    -32768, 32767).astype(np.int16)
    data = _bucketize(mixed, buckets)
    _write_cache(cache, [p1, p2], data, buckets)
    return data


def _cache_valid(cache: Path, files: list, buckets: int) -> bool:
    if not cache.exists():
        return False
    try:
        import json as _json
        data = _json.loads(cache.read_text(encoding="utf-8"))
        return (data.get("src_sizes") == [f.stat().st_size for f in files]
                and data.get("src_mtimes") == [f.stat().st_mtime_ns for f in files]
                and data.get("buckets") == buckets)
    except Exception:
        return False


def _write_cache(cache: Path, files: list, data: dict, buckets: int) -> None:
    import json as _json
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        payload = {**data, "src_sizes": [f.stat().st_size for f in files],
                   "src_mtimes": [f.stat().st_mtime_ns for f in files],
                   "buckets": buckets}
        tmp = cache.with_suffix(".json.tmp")
        try:
            tmp.write_text(_json.dumps(payload), encoding="utf-8")
            atomic_replace(tmp, cache)
        finally:
            tmp.unlink(missing_ok=True)
    except Exception:
        pass                          # 缓存写失败不影响返回
