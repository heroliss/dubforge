"""项目 API: CRUD (M1; M4 前端接手)"""
import json
import threading
from pathlib import Path
from typing import Literal

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.registry import PIPELINE_KEYS, STEP_KEYS, STEP_MAP
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/projects", tags=["projects"])
_CHAIN_LOCK = threading.RLock()


def _same_param_value(left, right) -> bool:
    """比较项目覆盖值与全局默认值的语义相等性。

    参数来自 JSON；bool 需要单独处理，因为 Python 中 ``True == 1``，但
    配置语义上不能把布尔值和数字当作同一个值。列表/字典使用结构化相等性。
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


class ProjectCreate(BaseModel):
    title: str = ""
    source_url: str = ""
    source_kind: Literal["link", "youtube", "local"] = "link"
    # link/youtube (yt-dlp 支持的任意视频站点) | local
    params: dict = Field(default_factory=dict)


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
    source_url = body.source_url.strip()
    if body.source_kind == "local":
        source_url = source_url.strip('"')
    try:
        project = crud.create_project(title=title, source_url=source_url,
                                      source_kind=body.source_kind, params=body.params)
    except DubForgeError:
        raise
    except Exception as e:
        raise DubForgeError(f"创建项目失败: {e}", code="PROJECT_CREATE_FAIL")
    return _project_view(project)


@router.post("/{uid}/ingest")
async def ingest_video(uid: str, request: Request, filename: str = "video.mp4"):
    """拖入导入: 项目创建后把文件内容流式写进项目目录 media/。
    浏览器拿不到拖入文件的真实磁盘路径, 只能传内容 —— 先建项目目录作容器,
    字节流直写最终位置 (不落收件箱/临时目录), 副本即项目自有文件, 随项目存亡。"""
    import re
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    if project.source_kind != "local":
        raise DubForgeError("只有本地导入项目可以接收文件内容", code="INGEST_KIND_INVALID")
    from dubforge.utils.fs import atomic_replace

    from app.api.files import VIDEO_EXTS
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", filename).strip()
    # "."开头 (隐藏文件/纯点) 与无扩展名一律归一为 video.mp4 —— dst 拼进 media/
    # 时绝不能出现 ".."/"." 这类路径组件
    if not name or name.startswith(".") or "." not in name:
        name = "video.mp4"
    if Path(name).suffix.lower() not in VIDEO_EXTS:
        raise DubForgeError("只支持常见视频文件格式", code="INGEST_FORMAT_INVALID",
                            suggestion="请选择 mp4、mkv、mov、webm 等视频文件。")
    pdir = crud.project_dir(uid)
    media = pdir / "media"
    media.mkdir(parents=True, exist_ok=True)
    dst = media / name
    tmp = media / f".tmp_ingest_{name}"
    size = 0
    try:
        with tmp.open("wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                size += len(chunk)
        if not size:
            raise DubForgeError("文件内容为空", code="INGEST_EMPTY")
        # 只有完整内容落到临时文件后才替换正式源文件，避免上传中断留下
        # 一个看似有效的 video.* 供下游误用。
        atomic_replace(tmp, dst)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    # 单一源视频不变式: 新文件已就位后再清掉其他旧副本。
    for old in media.iterdir():
        if old != dst and old.is_file() and old.suffix.lower() in VIDEO_EXTS:
            old.unlink(missing_ok=True)
    # 上传耗时可达数分钟: 合并前重取项目, 避免旧快照覆盖期间的链计划写入。
    latest = crud.get_project(uid)
    if latest is None:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    latest.source_url = dst.relative_to(pdir).as_posix()    # 相对路径 = 项目目录内副本
    crud.save_project(latest)
    crud.write_manifest(uid)
    return {"ok": True, "file": latest.source_url, "size": size}


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
    # 只改标题列: 整行 merge 会用旧快照覆盖 params 里的链计划 (ADR-0075)。
    crud.update_project_title(uid, title)
    crud.write_manifest(uid)
    return _project_view(crud.get_project(uid))


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
    # 前端确认过的步骤快照；旧调用方未传时仍使用项目已保存的勾选。
    steps_by_uid: dict[str, list[str]] | None = None


def _auto_chain_of(project) -> list[str]:
    from dubforge.pipeline.dependencies import selected_steps
    return selected_steps(project)


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
            chain = (body.steps_by_uid.get(uid, []) if body.steps_by_uid is not None
                     else _auto_chain_of(project))
            if not chain:
                results.append({"uid": uid, "skipped": "未勾选任何步骤"})
                continue
            result = run_chain(uid, RunChain(steps=chain))
            if result.get("status") == "already_running":
                results.append({"uid": uid, "skipped": result["message"]})
                continue
            results.append({"uid": uid, "started": STEP_MAP[result["started"]].label
                            if result.get("started") else "全部已是最新, 跳过"})
        except DubForgeError as e:
            results.append({"uid": uid, "skipped": e.message})
    started_n = sum(
        1 for r in results
        if r.get("started") and r["started"] != "全部已是最新, 跳过"
    )
    return {"ok": True, "started": started_n, "results": results}


class AutoSteps(BaseModel):
    steps: list[str]


@router.put("/{uid}/auto_steps")
def set_auto_steps(uid: str, body: AutoSteps):
    """保存项目的勾选步骤集 (链式执行的依据; 未保存过 = 全部)"""
    from dubforge.pipeline import dubbing_session
    with dubbing_session.gate:
        return _set_auto_steps(uid, body)


def _set_auto_steps(uid: str, body: AutoSteps):
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    valid = [k for k in STEP_KEYS if k != "dubtask" and k in set(body.steps)]
    # 与链写入共用同一把门禁, params 读改写不会互相覆盖。
    from dubforge.pipeline import dubbing_session
    with dubbing_session.gate:
        params = crud.project_params(project)
        params["auto_steps"] = valid
        project.params = json.dumps(params, ensure_ascii=False)
        crud.save_project(project)
    crud.write_manifest(uid)
    return {"ok": True, "auto_steps": valid}


class RunChain(BaseModel):
    steps: list[str]
    include_dependencies: bool = False


@router.post("/{uid}/run_plan")
def run_plan(uid: str, body: RunChain):
    from dubforge.pipeline.dependencies import Dependencies
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    deps = Dependencies(project)
    versions = []
    auto_comments = False
    if 'assemble' in body.steps:
        from dubforge.pipeline import assembly_versions
        from dubforge import comments_auto
        definitions = assembly_versions.catalog(uid)['versions']
        # 评论准备=auto 且无已确认方案时不再拦截规划 (ADR-0073):
        # 合成步骤会自动采集; saved 模式仍维持「请先确认」。
        overlay, auto_comments = comments_auto.confirmed_or_auto(uid, project)             if any(v['include_comments'] for v in definitions) else (None, False)
        versions = [assembly_versions.resolve(deps, v, comment_plan=overlay) for v in definitions]
        deps = deps.with_versions(versions)
    plan = deps.plan(body.steps, body.include_dependencies)
    effects = []
    if "tts" in plan["steps"]:
        effects.append("配音按全项目声源选择处理，复用仍有效的音频；不读取句子列表的临时勾选。")
    if "assemble" in plan["steps"]:
        effects.append('生成已保存版本：' + '、'.join(v['name'] for v in versions) + '；不读取成品卡片临时勾选。')
        if auto_comments:
            effects.append('评论准备为自动采集：合成时将自动抓取、评分并翻译上屏评论。')
    return {**plan, "labels": {k: STEP_MAP[k].label for k in plan["steps"]}, "effects": effects}



@router.post("/{uid}/run_chain")
def run_chain(uid: str, body: RunChain):
    # 计划登记和首步入队一起串行处理，重复点击不能覆盖正在运行的队列。
    from dubforge.runner import runner
    from dubforge.pipeline import dubbing_session
    with _CHAIN_LOCK, runner._start_lock, dubbing_session.gate:
        from dubforge.workflows import check_idle
        check_idle(uid)
        return _run_chain(uid, body)


def _run_chain(uid: str, body: RunChain):
    """运行勾选链: 从 steps[0] 起, 每步完成后自动续跑下一个,
    直到链尾; 失败/取消即停止。steps 必须按管线顺序。"""
    from dubforge.runner import runner
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    if not body.steps:
        raise DubForgeError("没有勾选任何步骤", code="CHAIN_EMPTY",
                            suggestion="勾选要执行的步骤后点击「运行所选」。")
    try:
        idxs = [STEP_KEYS.index(k) for k in body.steps]
    except ValueError:
        raise DubForgeError("包含未知步骤", code="CHAIN_INVALID")
    if idxs != sorted(idxs) or len(set(idxs)) != len(idxs):
        raise DubForgeError("步骤必须按显示顺序排列且不能重复", code="CHAIN_INVALID")
    # 页面刷新/后退不会主动重发命令，但用户可能在请求仍在返回时再次
    # 提交同一条链。先做幂等门禁，不能让重复请求把正在运行的 _chain
    # 清空，否则首步完成后会丢失后续自动步骤。
    active_step = next((s for s in crud.get_steps(project.id)
                        if s.status in {"running", "queued"}), None)
    if active_step:
        return {"ok": True, "status": "already_running",
                "started": active_step.step_key, "stopped": None,
                "message": "项目已有任务运行中，保留原执行计划，未重复提交"}
    plan = run_plan(uid, body)
    if plan["blockers"]:
        item = plan["blockers"][0]
        raise DubForgeError(f"上游依赖未就绪：{item['name']}，{item['reason']}，来自「{item['label']}」",
                            code="DEPS_NOT_MET", suggestion="缺失产物需生成，过期产物需更新；可使用「准备依赖并运行」。")
    body = body.model_copy(update={"steps": plan["steps"]})
    params = crud.project_params(project)
    # 链头步骤保留在计划内: 成功后才出队, 失败/中断时计划仍完整可恢复。
    params["_chain"] = body.steps
    params["_chain_state"] = "active"
    params.pop("_chain_hold", None)
    project.params = json.dumps(params, ensure_ascii=False)
    crud.save_project(project)
    try:
        r = runner.start_step(uid, body.steps[0])
    except DubForgeError as e:
        # 竞态下另一条相同请求可能已经把首步置为 queued/running；此时
        # 保留它的链队列，避免重复提交破坏自动续跑。真正的启动错误改为
        # 挂起计划 (首步压回链头), 与链中失败同语义, 可一键恢复。
        current = crud.get_step(project.id, body.steps[0])
        if not current or current.status not in ("running", "queued"):
            runner._hold_chain(project, body.steps[0], e.code,
                               f"「{STEP_MAP[body.steps[0]].label}」无法启动: {e.message}")
        raise
    if r["status"] == "skipped":               # 首步已最新 → 继续链
        info = runner.continue_chain(project)
        return {"ok": True, "started": info["started"], "stopped": info["stopped"]}
    return {"ok": True, "started": body.steps[0], "stopped": None}


@router.post("/{uid}/chain/resume")
def chain_resume(uid: str):
    """继续挂起的自动运行计划: 重新激活并从链头执行 (依赖未就绪会再次挂起)。"""
    from dubforge.runner import runner
    from dubforge.pipeline import dubbing_session
    with _CHAIN_LOCK, runner._start_lock, dubbing_session.gate:
        from dubforge.workflows import check_idle
        check_idle(uid)
        project = crud.get_project(uid)
        if not project:
            raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
        params = crud.project_params(project)
        if (not isinstance(params.get("_chain"), list) or not params.get("_chain")
                or params.get("_chain_state") != "held"):
            raise DubForgeError("没有挂起的自动运行计划", code="CHAIN_NOT_HELD",
                                suggestion="链在自动运行中或不存在; 可在总览页勾选步骤运行。")
        active = next((s for s in crud.get_steps(project.id)
                       if s.status in {"running", "queued"}), None)
        if active:
            raise DubForgeError("项目已有任务运行中", code="PIPELINE_BUSY",
                                suggestion="等当前步骤结束后再继续剩余计划。")
        params["_chain_state"] = "active"
        params.pop("_chain_hold", None)
        project.params = json.dumps(params, ensure_ascii=False)
        crud.save_project(project)
        info = runner.continue_chain(project)
        return {"ok": True, "started": info["started"], "stopped": info["stopped"]}


@router.post("/{uid}/chain/stop")
def chain_stop(uid: str):
    """明确放弃自动运行计划 (当前正在运行的步骤不受影响, 跑完即止)。"""
    from dubforge.runner import runner
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    runner._clear_chain(project, "用户放弃自动运行计划")
    return {"ok": True}


@router.get("/{uid}/step_params")
def get_project_step_params(uid: str):
    """项目覆盖逐级继承系列及全局；defaults 与 parent_sources 表示恢复继承后的值和来源。"""
    from dubforge import config as cfg
    from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA, schema_view
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    from dubforge import configuration
    settings = cfg.load_config()
    resolved = configuration.resolve(uid, settings=settings)
    ov = resolved['overrides']
    values, overridden, defaults = {}, {}, {}
    from dubforge.pipeline.step_params import language_capabilities
    caps = language_capabilities(settings=settings)
    for fields in STEP_PARAM_SCHEMA.values():
        for f in fields:
            defaults[f.path] = resolved['defaults'][f.path]
            values[f.path] = resolved['values'][f.path]
            overridden[f.path] = f.path in ov
    return {"ok": True, "steps": schema_view(values, caps, settings=settings),
            "overridden": overridden, "defaults": defaults, "caps": caps,
            "origins": resolved['origins'], "parent_origins": resolved['parent_origins'],
            "sources": resolved['sources'], "parent_sources": resolved['parent_sources'],
            "revision": resolved['revision'], "fingerprint": resolved['fingerprint']}


class StepParamsUpdate(BaseModel):
    updates: dict = Field(default_factory=dict)        # 点分路径 → 值 (写为本项目覆盖)
    reset: list[str] = Field(default_factory=list)     # 恢复跟随全局默认的路径
    revision: int | None = None
    fingerprint: str | None = None


@router.put("/{uid}/step_params")
def update_project_step_params(uid: str, body: StepParamsUpdate):
    """保存本项目的步骤参数覆盖 (全局默认不动)。清空的键恢复跟随全局。"""
    # 配置入口统一负责与任务登记、逐句编辑互斥，避免反向取锁。
    return _update_project_step_params(uid, body)


def _update_project_step_params(uid: str, body: StepParamsUpdate):
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    from dubforge import configuration
    current = configuration.resolve(uid)
    result = configuration.save('project:' + uid, dict(updates=body.updates, reset=body.reset,
        revision=body.revision if body.revision is not None else current['revision'], fingerprint=body.fingerprint))
    crud.write_manifest(uid)          # params 已入 manifest, 重建索引时一并恢复
    return {"ok": True, "overridden": sorted(result['overrides']), "revision": result['revision']}


@router.post("/{uid}/reveal")
def reveal_project(uid: str):
    """在系统文件资源管理器中显示项目目录 (服务端即本机)"""
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    pdir = crud.project_dir(uid)
    if not pdir.exists():
        raise DubForgeError("项目目录不存在", code="PROJECT_DIR_MISSING")
    from dubforge.utils.reveal import reveal_in_explorer
    reveal_in_explorer(str(pdir))
    return {"ok": True, "dir": str(pdir)}


@router.get("/{uid}/files")
def project_files(uid: str):
    """项目目录文件清单 (总览文件浏览; 前端自行排序)"""
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    pdir = crud.project_dir(uid)
    from dubforge.project_files import list_files
    return list_files(pdir)


@router.delete("/{uid}")
def delete_project(uid: str, force: bool = False):
    """删除项目; 有运行中/排队步骤时默认拒绝 (force=true: 取消并等退出, 每阶段 8s;
    温和取消超时自动升级强制结束 —— 子进程立即终止, 进程内计算后台收尾后结果丢弃)"""
    import time
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    active = [s for s in crud.get_steps(project.id)
              if s.status in ("running", "queued") and s.step_key in STEP_MAP]
    if active:
        names = ", ".join(f"「{STEP_MAP[s.step_key].label}」" for s in active)
        if not force:
            raise DubForgeError(
                f"项目正在运行或排队: {names}, 不能直接删除",
                code="PROJECT_BUSY",
                suggestion="先取消运行中的步骤再删除; 或在确认框中选择「取消任务并删除」。")
        from dubforge.runner import runner as _runner

        def _still_active() -> list:
            return [s for s in crud.get_steps(project.id)
                    if s.status in ("running", "queued")]

        for s in active:
            _runner.cancel_step(uid, s.step_key)
        if _wait_idle(_still_active, time.time() + 8):
            # 温和取消没退完 (如 demucs 进程内计算) → 强制结束: 状态立即落定
            for s in _still_active():
                _runner.force_stop(uid, s.step_key)
            _wait_idle(_still_active, time.time() + 8)
        if _still_active():
            raise DubForgeError("任务尚未退出 (后台可能仍在收尾); 请稍后再试",
                                code="PROJECT_DELETE_TIMEOUT")
    if not crud.delete_project(uid):
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    return {"ok": True}


def _wait_idle(still_active, deadline: float) -> bool:
    """等待步骤全部退出; 超时返回 True (调用方决定升级/报错)"""
    import time
    while still_active():
        if time.time() >= deadline:
            return True
        time.sleep(0.3)
    return False


def _workflow_status_of(uid: str) -> str | None:
    """最近一次全流程的状态 (轻量: 单行索引查询, 供列表徽标区分运行中项目)。"""
    from sqlmodel import Session, select
    from dubforge.db import get_engine
    from dubforge.workflow_models import WorkflowRun
    with Session(get_engine()) as session:
        row = session.exec(select(WorkflowRun).where(WorkflowRun.project_uid == uid)
                           .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc()).limit(1)).first()
        return row.status if row else None


def _chain_view(project) -> dict | None:
    """自动续跑链对外视图: 计划与活性分离, held = 挂起可恢复 (ADR-0072)。"""
    params = crud.project_params(project)
    chain = params.get("_chain")
    if not isinstance(chain, list) or not chain:
        return None
    view = {"remaining": [k for k in chain if k in STEP_MAP],
            "state": "held" if params.get("_chain_state") == "held" else "active"}
    hold = params.get("_chain_hold")
    if isinstance(hold, dict):
        view["hold"] = {"step": hold.get("step"), "code": hold.get("code"),
                        "message": hold.get("message")}
    return view


def _project_view(project) -> dict:
    # 旧版本或手工 manifest 可能留下未知步骤；项目列表仍应可用，未知行由
    # 重建/迁移流程保留但不让 API 因 STEP_MAP 缺项整体 500。
    from dubforge.pipeline.dependencies import Dependencies, project_status, selected_steps
    deps = Dependencies(project)
    steps = [s for s in deps.steps.values() if s.step_key in STEP_MAP]
    selected = selected_steps(project)
    running = next((s.step_key for s in steps
                    if s.status in ("running", "queued")), None)
    from dubforge.runner import runner
    step_views_by_key = {s.step_key: {"key": s.step_key, "label": STEP_MAP[s.step_key].label,
                   "status": deps.status(s.step_key),
                   "progress": 100.0 if deps.status(s.step_key) == "done" else round(s.progress or 0, 1),
                   "selected": s.step_key in selected, "fresh": deps.display_fresh(s.step_key),
                   "error": s.error_message if deps.status(s.step_key) != "done" else None,
                   **(runner.progress_eta(project.id, s.step_key)
                      if s.status == "running" else {})} for s in steps}
    step_views = [step_views_by_key[key] for key in PIPELINE_KEYS
                  if key in step_views_by_key]
    return {
        "uid": project.uid,
        "title": project.title,
        "source_url": project.source_url,
        "source_kind": project.source_kind,
        "status": project_status(project, deps),
        "running_step": running,
        "running_label": STEP_MAP[running].label if running else None,
        "params": crud.project_params(project),
        "chain": _chain_view(project),
        "workflow_status": _workflow_status_of(project.uid),
        "created_at": project.created_at.isoformat(),
        # 项目目录是否还在磁盘上 (被移走/删除 → 列表标注「目录缺失」)
        "dir_exists": deps.root.exists(),
        # 分段进度条数据源: 每步状态+进度
        "steps": step_views,
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
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, check=False)
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
