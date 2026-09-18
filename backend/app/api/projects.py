# -*- coding: utf-8 -*-
"""项目 API: CRUD (M1; M4 前端接手)"""
import json

from fastapi import APIRouter
from pydantic import BaseModel

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.registry import STEP_KEYS, STEP_MAP

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    title: str = ""
    source_url: str = ""
    source_kind: str = "link"         # link (yt-dlp 支持的任意视频站点) | local
    params: dict = {}


def _auto_title(body: ProjectCreate) -> str:
    """标题留空时的自动命名: 链接 → yt-dlp 取视频标题 (不下载, 快速元数据),
    失败退回「站点_视频ID」; 本地 → 文件名。不用随机字符。"""
    if body.source_kind == "local":
        from pathlib import Path
        return Path(body.source_url.strip().strip('"')).stem or "本地视频"
    try:
        import yt_dlp
        from dubforge import config
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                "extract_flat": True, "socket_timeout": 12, "trust_env": False}
        cookies = config.get("download.cookies_path")
        if cookies:
            opts["cookiefile"] = cookies
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(body.source_url.strip(), download=False)
        if info and info.get("title"):
            return str(info["title"])
    except Exception:
        pass
    # 兜底: 站点名 + 视频 ID / 路径段 (可读, 非随机)
    from urllib.parse import parse_qs, urlparse
    u = urlparse(body.source_url.strip())
    vid = parse_qs(u.query).get("v", [""])[0] or u.path.rstrip("/").rsplit("/", 1)[-1]
    host = ".".join(u.netloc.split(".")[-2:]) if u.netloc else ""
    return f"{host}_{vid}" if (host and vid) else (host or "链接项目")


@router.post("")
def create_project(body: ProjectCreate):
    title = body.title.strip() or _auto_title(body)
    project = crud.create_project(title=title, source_url=body.source_url,
                                  source_kind=body.source_kind, params=body.params)
    return _project_view(project)


class ProjectRename(BaseModel):
    title: str


@router.patch("/{uid}")
def rename_project(uid: str, body: ProjectRename):
    """改名: 只改标题, 不动任何产物 (manifest 同步刷新)"""
    title = body.title.strip()[:200]
    if not title:
        raise DubForgeError("标题不能为空", code="TITLE_EMPTY",
                            suggestion="请输入一个能认出这个视频的名字。")
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    project.title = title
    crud.save_project(project)
    crud.write_manifest(uid)
    return _project_view(project)


@router.get("")
def list_projects():
    return [_project_view(p) for p in crud.list_projects()]


@router.get("/{uid}")
def get_project(uid: str):
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    return _project_view(project)


class RunMany(BaseModel):
    uids: list[str]


def _auto_chain_of(project) -> list[str]:
    """项目的勾选链: params.auto_steps (未自定义 = 全部步骤)。
    语义: 从第一个勾选步骤起, 连续勾选段; 段内已完成的由链自动跳过。"""
    params = json.loads(project.params or "{}")
    auto = params.get("auto_steps")
    if auto is None:
        return list(STEP_KEYS)
    auto_set = set(auto)
    chain: list[str] = []
    started_collecting = False
    for k in STEP_KEYS:
        if k in auto_set:
            started_collecting = True
            chain.append(k)
        elif started_collecting:
            break
    return chain


@router.post("/run_many")
def run_many(body: RunMany):
    """批量运行勾选的项目: 每个项目按其勾选链启动 (跳过已完成/进行中);
    实际并发度由 runner.step_concurrency 按步骤控制 (GPU 类自动排队)"""
    from dubforge.runner import runner
    results = []
    for uid in body.uids:
        try:
            project = crud.get_project(uid)
            if not project:
                results.append({"uid": uid, "skipped": "项目不存在"})
                continue
            steps = {s.step_key: s for s in crud.get_steps(project.id)}
            if all(steps[k].status == "done" for k in STEP_KEYS):
                results.append({"uid": uid, "skipped": "已完成, 跳过"})
                continue
            chain = _auto_chain_of(project)
            if not chain:
                results.append({"uid": uid, "skipped": "未勾选任何步骤"})
                continue
            head = chain[0]
            if steps[head].status in ("running", "queued"):
                results.append({"uid": uid,
                                "skipped": f"「{STEP_MAP[head].label}」进行中"})
                continue
            # 剩余链入队 (首步由 start_step 启动)
            params = json.loads(project.params or "{}")
            params["_chain"] = chain[1:]
            project.params = json.dumps(params, ensure_ascii=False)
            crud.save_project(project)
            r = runner.start_step(uid, head)
            if r["status"] == "skipped":      # 首步已最新 → 让链继续
                info = runner.continue_chain(project)
                results.append({"uid": uid,
                                "started": STEP_MAP[info["started"]].label
                                if info["started"] else "全部已是最新, 跳过"})
            else:
                results.append({"uid": uid, "started": STEP_MAP[head].label})
        except DubForgeError as e:
            results.append({"uid": uid, "skipped": e.message})
    started_n = sum(1 for r in results if "started" in r)
    return {"ok": True, "started": started_n, "results": results}


class AutoSteps(BaseModel):
    steps: list[str]


@router.put("/{uid}/auto_steps")
def set_auto_steps(uid: str, body: AutoSteps):
    """保存项目的勾选步骤集 (链式执行的依据; 未保存过 = 全部)"""
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    valid = [k for k in STEP_KEYS if k in set(body.steps)]
    params = json.loads(project.params or "{}")
    params["auto_steps"] = valid
    project.params = json.dumps(params, ensure_ascii=False)
    crud.save_project(project)
    return {"ok": True, "auto_steps": valid}


class RunChain(BaseModel):
    steps: list[str]


@router.post("/{uid}/run_chain")
def run_chain(uid: str, body: RunChain):
    """运行勾选链: 从 steps[0] 起, 每步完成后自动续跑下一个,
    直到链尾; 失败/取消即停止。steps 必须按管线顺序。"""
    from dubforge.runner import runner
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    if not body.steps:
        raise DubForgeError("没有勾选任何步骤", code="CHAIN_EMPTY",
                            suggestion="勾选要自动执行的步骤后再点「运行本步及后续」。")
    try:
        idxs = [STEP_KEYS.index(k) for k in body.steps]
    except ValueError:
        raise DubForgeError("包含未知步骤", code="CHAIN_INVALID")
    if idxs != sorted(idxs) or len(set(idxs)) != len(idxs):
        raise DubForgeError("勾选链必须按管线顺序连续排列", code="CHAIN_INVALID")
    params = json.loads(project.params or "{}")
    params["_chain"] = body.steps[1:]
    project.params = json.dumps(params, ensure_ascii=False)
    crud.save_project(project)
    try:
        r = runner.start_step(uid, body.steps[0])
    except DubForgeError:
        params["_chain"] = []
        project.params = json.dumps(params, ensure_ascii=False)
        crud.save_project(project)
        raise
    if r["status"] == "skipped":               # 首步已最新 → 继续链
        info = runner.continue_chain(project)
        return {"ok": True, "started": info["started"], "stopped": info["stopped"]}
    return {"ok": True, "started": body.steps[0], "stopped": None}


@router.post("/{uid}/reveal")
def reveal_project(uid: str):
    """在系统文件资源管理器中显示项目目录 (服务端即本机)"""
    import subprocess
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    pdir = crud.project_dir(uid)
    if not pdir.exists():
        raise DubForgeError("项目目录不存在", code="PROJECT_DIR_MISSING")
    subprocess.Popen(["explorer", f"/select,{pdir}"])
    return {"ok": True, "dir": str(pdir)}


@router.get("/{uid}/files")
def project_files(uid: str):
    """项目目录文件清单 (总览文件浏览; 前端自行排序)"""
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    pdir = crud.project_dir(uid)
    entries = []
    if pdir.exists():
        for p in sorted(pdir.rglob("*")):
            if p.is_file():
                st = p.stat()
                entries.append({"file": str(p.relative_to(pdir)),
                                "size": st.st_size,
                                "mtime": st.st_mtime,
                                "ctime": st.st_ctime})
    return {"dir": str(pdir), "entries": entries[:2000]}


@router.delete("/{uid}")
def delete_project(uid: str, force: bool = False):
    """删除项目; 有运行中/排队步骤时默认拒绝 (force=true: 先取消并等退出, 最多 20s)"""
    import time
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    active = [s for s in crud.get_steps(project.id)
              if s.status in ("running", "queued")]
    if active:
        names = ", ".join(f"「{STEP_MAP[s.step_key].label}」" for s in active)
        if not force:
            raise DubForgeError(
                f"项目正在运行或排队: {names}, 不能直接删除",
                code="PROJECT_BUSY",
                suggestion="先取消运行中的步骤再删除; 或在确认框中选择「取消任务并删除」。")
        from dubforge.runner import runner as _runner
        for s in active:
            _runner.cancel_step(uid, s.step_key)
        deadline = time.time() + 20
        while time.time() < deadline:
            cur = [s for s in crud.get_steps(project.id)
                   if s.status in ("running", "queued")]
            if not cur:
                break
            time.sleep(0.4)
        else:
            raise DubForgeError("取消超时, 任务尚未退出; 请稍后再试",
                                code="PROJECT_DELETE_TIMEOUT")
    if not crud.delete_project(uid):
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    return {"ok": True}


def _project_view(project) -> dict:
    steps = crud.get_steps(project.id)
    running = next((s.step_key for s in steps
                    if s.status in ("running", "queued")), None)
    return {
        "uid": project.uid,
        "title": project.title,
        "source_url": project.source_url,
        "source_kind": project.source_kind,
        "status": project.status,
        "running_step": running,
        "running_label": STEP_MAP[running].label if running else None,
        "params": json.loads(project.params or "{}"),
        "created_at": project.created_at.isoformat(),
        # 分段进度条数据源: 每步状态+进度
        "steps": [{"key": s.step_key, "label": STEP_MAP[s.step_key].label,
                   "status": s.status, "progress": round(s.progress or 0, 1),
                   "error": s.error_message or None} for s in steps],
    }


@router.get("/{uid}/media")
def media_info(uid: str):
    """原视频信息 (总览页): 路径解析 + ffprobe 提取格式/分辨率/时长"""
    import subprocess

    from dubforge.pipeline.video_source import resolve_video
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    v = resolve_video(project, crud.project_dir(uid))
    if v is None:
        return {"found": False}
    info = {"found": True, "name": v.name, "size": v.stat().st_size,
            "path": str(v),
            "external": v.resolve().is_relative_to(crud.project_dir(uid).resolve()) is False}
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(v)],
            capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            d = json.loads(r.stdout or "{}")
            vs = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
            au = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), {})
            fmt = d.get("format", {})
            dur = fmt.get("duration")
            info.update({
                "container": fmt.get("format_name"),
                "duration": round(float(dur), 1) if dur else None,
                "width": vs.get("width"), "height": vs.get("height"),
                "vcodec": vs.get("codec_name"), "fps": vs.get("r_frame_rate"),
                "acodec": au.get("codec_name"), "sample_rate": au.get("sample_rate"),
            })
    except Exception:
        pass                      # ffprobe 不可用/超时: 只返回基础信息, 不阻塞总览
    return info
