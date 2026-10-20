"""项目 API: CRUD (M1; M4 前端接手)"""
import asyncio
import copy
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.project_archive import archived_at_iso
from dubforge.pipeline.registry import PIPELINE_KEYS, STEP_KEYS, STEP_MAP
from fastapi import APIRouter, Body, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/api/projects", tags=["projects"])
_CHAIN_LOCK = threading.RLock()
_RELEASE_UNSET = object()
_LIST_SINGLE_FLIGHT_WAIT = 25.0
_list_flights = {}
_list_flight_guard = threading.Lock()


def _workspace_id():
    import hashlib
    from dubforge import config
    identity = str(config.data_root().resolve()).casefold().encode('utf-8')
    return hashlib.sha256(identity).hexdigest()[:24]


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
    from dubforge.project_titles import placeholder
    return placeholder(body.source_url, body.source_kind)


@router.post("")
def create_project(body: ProjectCreate):
    title = body.title.strip() or _auto_title(body)
    source_url = body.source_url.strip()
    if body.source_kind == "local":
        source_url = source_url.strip('"')
    try:
        project = crud.create_project(title=title, source_url=source_url,
                                      source_kind=body.source_kind, params=body.params,
                                      title_mode="manual" if body.title.strip() else "auto")
    except DubForgeError:
        raise
    except Exception as e:
        raise DubForgeError(f"创建项目失败: {e}", code="PROJECT_CREATE_FAIL")
    return _project_view(project)


@router.post("/{uid}/ingest")
async def ingest_video(uid: str, request: Request, filename: str = "video.mp4"):
    from dubforge import video_info
    # 上传期间只保留项目占用标记，不跨 await 持有线程锁。
    operation = video_info.operation(uid, 'ingest')
    entered = False
    def begin():
        nonlocal entered
        operation.__enter__()
        entered = True
    try:
        await _ingest_io(begin)
        return await _ingest_video(uid, request, filename)
    finally:
        if entered:
            await _ingest_io(operation.__exit__, None, None, None)


async def _ingest_io(function, *args):
    """磁盘/数据库操作离开事件循环；取消后先等待本次写入退出再清理文件。"""
    pending = asyncio.create_task(run_in_threadpool(function, *args))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        # asyncio 原生取消也不能把仍在写入的线程遗留给 finally 的关闭/删除。
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not pending.cancelled():
            pending.exception()
        raise


async def _ingest_video(uid: str, request: Request, filename: str):
    """拖入导入: 项目创建后把文件内容流式写进项目目录 media/。
    浏览器拿不到拖入文件的真实磁盘路径, 只能传内容 —— 先建项目目录作容器,
    字节流直写最终位置 (不落收件箱/临时目录), 副本即项目自有文件, 随项目存亡。"""
    import re
    project = await _ingest_io(crud.get_project, uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    if project.source_kind != "local":
        raise DubForgeError("只有本地导入项目可以接收文件内容", code="INGEST_KIND_INVALID")
    from app.api.files import VIDEO_EXTS
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", filename).strip()
    # "."开头 (隐藏文件/纯点) 与无扩展名一律归一为 video.mp4 —— dst 拼进 media/
    # 时绝不能出现 ".."/"." 这类路径组件
    if not name or name.startswith(".") or "." not in name:
        name = "video.mp4"
    if Path(name).suffix.lower() not in VIDEO_EXTS:
        raise DubForgeError("只支持常见视频文件格式", code="INGEST_FORMAT_INVALID",
                            suggestion="请选择 mp4、mkv、mov、webm 等视频文件。")
    pdir = await _ingest_io(crud.project_dir, uid)
    media = pdir / "media"
    dst = media / name
    tmp = media / f".tmp_ingest_{name}"
    size = 0
    handle = None
    def begin_file():
        nonlocal handle
        media.mkdir(parents=True, exist_ok=True)
        handle = tmp.open('wb')
    def cleanup():
        try:
            if handle is not None:
                handle.close()
        finally:
            tmp.unlink(missing_ok=True)
    try:
        await _ingest_io(begin_file)
        async for chunk in request.stream():
            # 请求流始终在事件循环读取，只将当前有界字节块交给写入线程。
            await _ingest_io(handle.write, chunk)
            size += len(chunk)
        await _ingest_io(handle.close)
        if not size:
            raise DubForgeError("文件内容为空", code="INGEST_EMPTY")
        # 只有完整内容落到临时文件后才替换正式源文件，避免上传中断留下
        # 一个看似有效的 video.* 供下游误用。
        await _ingest_io(_commit_ingest, uid, pdir, media, tmp, dst)
    except BaseException:
        await _ingest_io(cleanup)
        raise

    return {"ok": True, "file": dst.relative_to(pdir).as_posix(), "size": size}


def _commit_ingest(uid, pdir, media, tmp, dst):
    """完整上传的本地提交；原子替换重试和清单写入均在线程中完成。"""
    from app.api.files import VIDEO_EXTS
    from dubforge.utils.fs import atomic_replace
    atomic_replace(tmp, dst)
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


class ProjectRename(BaseModel):
    title: str = ""
    title_mode: Literal["manual", "auto"] = "manual"


@router.patch("/{uid}")
def rename_project(uid: str, body: ProjectRename):
    """手工改名或恢复自动命名，不移动目录，不重建制作产物。"""
    from dubforge import runtime_edit
    with runtime_edit.editing('project:' + uid):
        project = crud.get_project(uid)
        if not project:
            raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
        if body.title_mode == "auto":
            from dubforge.project_titles import cached_title
            title = cached_title(project)
        else:
            title = body.title.strip()[:200]
            if not title:
                raise DubForgeError("标题不能为空", code="TITLE_EMPTY",
                                    suggestion="请输入一个能认出这个视频的名字。")
        # 只改名称相关列，避免旧快照覆盖自动流程计划。
        crud.update_project_title(uid, title, title_mode=body.title_mode)
        crud.write_manifest(uid)
        return _project_view(crud.get_project(uid))


@router.get("")
def list_projects(archive: str = 'active', response: Response = None, request: Request = None,
                  view: str = 'full'):
    """同一工作区合并在途读取；完成即释放，不缓存新鲜度结论。"""
    if view not in {'full', 'summary'}:
        raise DubForgeError('项目列表视图无效，请选择摘要或完整核对', code='PROJECT_LIST_VIEW_INVALID')
    workspace = _workspace_id()
    if response is not None:
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-DubForge-Workspace'] = workspace
        if view == 'summary':
            response.headers['X-DubForge-Projection'] = 'summary'
    # 摘要与完整核对是不同合同，不能复用另一种视图的在途结果。
    key = (workspace, archive) if view == 'full' else (workspace, archive, view)
    build = _build_project_summary if view == 'summary' else _build_project_list
    # 操作完成后的刷新不能加入操作前尚未返回的轮询，否则会把已删除、
    # 已归档的旧卡片重新带回来。这里只旁路本次请求，不取消其他读取。
    if request is not None and request.headers.get('X-DubForge-Refresh') == 'fresh':
        # 后续普通轮询也不能重新加入已经过时的读取；既有等待者持有的
        # event 仍由原计算唤醒，不中断正在读文件的线程。
        with _list_flight_guard:
            _list_flights.pop(key, None)
        return build(archive)
    with _list_flight_guard:
        flight = _list_flights.get(key)
        owner = flight is None
        if owner:
            flight = {'event': threading.Event(), 'value': None, 'error': None}
            _list_flights[key] = flight
    if not owner:
        if not flight['event'].wait(_LIST_SINGLE_FLIGHT_WAIT):
            raise DubForgeError('项目状态正在核对，请稍后重试', code='PROJECT_LIST_BUSY')
        if flight['error'] is not None:
            raise flight['error']
        return copy.deepcopy(flight['value'])
    try:
        value = build(archive)
    except BaseException as exc:
        with _list_flight_guard:
            flight['error'] = exc
            if _list_flights.get(key) is flight:
                _list_flights.pop(key, None)
            flight['event'].set()
        raise
    else:
        with _list_flight_guard:
            flight['value'] = value
            if _list_flights.get(key) is flight:
                _list_flights.pop(key, None)
            flight['event'].set()
        return value


def _build_project_summary(archive):
    from dubforge.project_list_summary import project_list
    return project_list(archive=archive)


def _build_project_list(archive):
    from dubforge.publishing.service import project_statuses
    from dubforge import comment_studio, release_batches
    projects = crud.list_projects(archive=archive)
    publications = project_statuses([p.uid for p in projects])
    batches = release_batches.project_statuses([p.uid for p in projects])
    comments = comment_studio.workspace_summaries(projects)
    from dubforge.runner import runner
    execution_jobs = runner.latest_execution_jobs(p.id for p in projects)
    workflow_statuses = _workflow_statuses_of([p.uid for p in projects])
    from dubforge.project_settings_summary import project_summaries
    settings_summaries = project_summaries(projects)
    from dubforge import project_batches
    project_batch_statuses = project_batches.project_statuses([p.uid for p in projects])
    # _project_view 会核对产物完整性、指纹和成片版本；这些检查必须保留，
    # 但不能让归档列表中的项目一个接一个阻塞整个响应。每个任务使用独立
    # SQLAlchemy session，线程数封顶，避免列表刷新把本地 SQLite 或 CPU 压满。
    if len(projects) <= 1:
        return [_project_view(p, publications[p.uid], batches.get(p.uid), execution_jobs=execution_jobs,
                              comments_workspace=comments[p.uid], workflow_status=workflow_statuses.get(p.uid),
                              settings_summary=settings_summaries[p.uid], project_batch=project_batch_statuses.get(p.uid)) for p in projects]
    def build_view(project):
        return _project_view(project, publications[project.uid], batches.get(project.uid), execution_jobs=execution_jobs,
                             comments_workspace=comments[project.uid], workflow_status=workflow_statuses.get(project.uid),
                             settings_summary=settings_summaries[project.uid], project_batch=project_batch_statuses.get(project.uid))
    with ThreadPoolExecutor(max_workers=min(4, len(projects)),
                            thread_name_prefix='project-list') as pool:
        return list(pool.map(build_view, projects))


@router.get('/context')
async def projects_context(response: Response):
    """只返回已绑定的数据目录身份，让列表快照不会串到另一个工作区。"""
    response.headers['Cache-Control'] = 'no-store'
    return {'workspace_id': _workspace_id()}


@router.get('/execution')
def project_execution_snapshot(response: Response, uids: str = ''):
    """高频进度只读任务记录和执行器内存，不核对逐句音频或产物新鲜度。"""
    from sqlmodel import select
    from dubforge.domain.models import Project, StepRun
    from dubforge.runner import runner

    requested = list(dict.fromkeys(value.strip() for value in uids.split(',') if value.strip()))
    if len(requested) > 100 or any(len(value) > 100 for value in requested):
        raise DubForgeError('单次最多刷新 100 个项目的运行进度', code='PROJECT_EXECUTION_SCOPE_INVALID')
    workspace = _workspace_id()
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-DubForge-Workspace'] = workspace
    if not requested:
        return {'workspace_id': workspace, 'projects': []}
    with crud.session() as session:
        projects = session.exec(select(Project.id, Project.uid).where(
            Project.uid.in_(requested), Project.archived_at.is_(None))).all()
        ids = [project.id for project in projects]
        steps = session.exec(select(StepRun.project_id, StepRun.step_key, StepRun.status,
            StepRun.progress).where(StepRun.project_id.in_(ids))).all() if ids else []
    jobs = runner.latest_execution_jobs(ids)
    grouped = {}
    for step in steps:
        if step.step_key in PIPELINE_KEYS:
            grouped.setdefault(step.project_id, []).append(step)
    result = []
    for project in projects:
        project_steps = grouped.get(project.id, [])
        executions = runner.execution_details(project.id, project_steps, jobs=jobs)
        rows = []
        for step in project_steps:
            job = jobs.get((project.id, step.step_key))
            progress = runner.progress_eta(project.id, step.step_key) if step.status == 'running' else {
                'eta_seconds': 0, 'eta_scope': 'step', 'progress_estimated': False,
                'progress_phase': '', 'progress_indeterminate': False}
            # execution_details 负责当前任务身份与重试/中断状态，不能被空的内存快照覆盖。
            progress.pop('execution', None)
            rows.append(dict(key=step.step_key, status=step.status, progress=round(step.progress or 0, 1),
                job_id=job.id if job else None, execution=executions.get(step.step_key), **progress))
        running = next((row['key'] for row in rows if row['status'] in {'running', 'queued'}), None)
        result.append(dict(uid=project.uid, steps=rows, running_step=running,
                           running_label=STEP_MAP[running].label if running else None))
    return {'workspace_id': workspace, 'projects': result}


@router.put('/{uid}/archive')
def archive_project(uid: str, body: object = Body(default=None)):
    """归档保留目录和制作结果；恢复后重新回到活动项目列表。"""
    from dubforge.project_archive import ProjectArchiveError, set_archived
    from dubforge.runtime_edit import RuntimeEditError
    if not isinstance(body, dict) or set(body) != {'archived'} or type(body['archived']) is not bool:
        raise ProjectArchiveError('归档请求只需包含 archived 字段，值必须为 true 或 false')
    try:
        project = set_archived(uid, body['archived'])
    except RuntimeEditError as exc:
        action = '归档' if body['archived'] else '恢复'
        raise RuntimeEditError(f'项目有任务尚未结束，暂不能{action}。',
            suggestion='请等待制作、上传及后台收尾结束后重试。', detail=exc.detail) from exc
    return dict(ok=True, uid=project.uid, archived=project.archived_at is not None,
                archived_at=archived_at_iso(project.archived_at))


@router.get("/{uid}")
def get_project(uid: str):
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    return _project_view(project)


@router.get('/{uid}/navigation')
def project_navigation(uid: str, response: Response):
    """导航只取本地项目摘要；制作有效性由同批步骤状态提供。"""
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f'项目不存在: {uid}', code='PROJECT_NOT_FOUND')
    response.headers['Cache-Control'] = 'no-store'
    from dubforge import release_batches
    params = crud.project_params(project)
    selected = params.get('auto_steps')
    return dict(title=project.title, status=project.status,
                archived=project.archived_at is not None,
                archived_at=archived_at_iso(project.archived_at),
                dir_exists=crud.project_dir_by_project(project).exists(),
                auto_steps=[key for key in selected if key in PIPELINE_KEYS] if isinstance(selected, list) else None,
                chain=_chain_view(project, params=params), release_batch=release_batches.project_status(uid))


class RunMany(BaseModel):
    uids: list[str]
    # 前端确认过的步骤快照；旧调用方未传时仍使用项目已保存的勾选。
    steps_by_uid: dict[str, list[str]] | None = None
    dubbing_by_uid: dict[str, dict] | None = None
    plan_fingerprints_by_uid: dict[str, str] | None = None


def _auto_chain_of(project) -> list[str]:
    from dubforge.pipeline.dependencies import selected_steps
    return selected_steps(project)


@router.post("/run_many")
def run_many(body: RunMany):
    """批量运行勾选的项目: 每个项目按其勾选链启动 (跳过已完成/进行中);
    实际并发度由 runner.step_concurrency 按步骤控制 (GPU 类自动排队)"""
    from dubforge.runner import runner
    from dubforge.project_archive import assert_active
    results = []
    for uid in body.uids:
        try:
            project = crud.get_project(uid)
            if not project:
                results.append({"uid": uid, "skipped": "项目不存在"})
                continue
            assert_active(uid)
            chain = (body.steps_by_uid.get(uid, []) if body.steps_by_uid is not None
                     else _auto_chain_of(project))
            if not chain:
                results.append({"uid": uid, "skipped": "未勾选任何步骤"})
                continue
            result = run_chain(uid, RunChain(steps=chain,
                dubbing=(body.dubbing_by_uid or {}).get(uid),
                plan_fingerprint=(body.plan_fingerprints_by_uid or {}).get(uid)))
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
    # 勾选只影响下一次/下一条尚未启动的链，不改变当前步骤的执行合同。
    # 因此运行期间也允许调整，真正启动新步骤时仍由 Runner 做资源门禁。
    return _set_auto_steps(uid, body)


def _set_auto_steps(uid: str, body: AutoSteps):
    # 与归档共用同一顺序的短门，避免 assert_active 后归档请求抢先提交，
    # 随后旧页面又把 auto_steps 写回归档项目。运行中的活动项目仍允许调整
    # 下一条尚未启动的链，故这里只检查归档状态，不复用项目级运行门禁。
    from dubforge.project_archive import assert_active
    from dubforge.pipeline import dubbing_session
    from dubforge.runner import runner
    with runner._start_lock, dubbing_session.gate:
        project = crud.get_project(uid)
        if not project:
            raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
        assert_active(uid)
        valid = [k for k in STEP_KEYS if k != "dubtask" and k in set(body.steps)]
        # 与链写入共用同一把门禁, params 读改写不会互相覆盖。
        params = crud.project_params(project)
        params["auto_steps"] = valid
        project.params = json.dumps(params, ensure_ascii=False)
        crud.save_project(project)
    crud.write_manifest(uid)
    return {"ok": True, "auto_steps": valid}


class RunChain(BaseModel):
    steps: list[str]
    include_dependencies: bool = False
    dubbing: dict | None = None
    plan_fingerprint: str | None = None


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
    dubbing = None
    if 'tts' in plan['steps'] or ('assemble' in plan['steps'] and any(v.get('dubbing') for v in versions)):
        from dubforge.pipeline import dubbing_policy
        dubbing = dubbing_policy.preview(project, dubbing_policy.current(body.dubbing), dependencies=deps)
        if (dubbing['policy']['mode'] != 'fill' or dubbing.get('repair')) and 'tts' not in plan['steps']:
            plan['steps'] = [key for key in STEP_KEYS if key in set(plan['steps']) | {'tts'}]
            plan['added'].append('tts')
        from dubforge.workflow_plan import dubbing_dependencies
        plan = dubbing_dependencies(deps, plan, dubbing, body.include_dependencies)
        if 'assemble' in plan['steps']:
            from dubforge.workflow_plan import production_checks
            for issue in production_checks(deps, ['assemble'], dubbing_summary=dubbing):
                plan['blockers'].append(dict(step='tts', consumer='assemble', label='字幕与配音',
                    name='配音排布', reason=issue, state='conflict', ready=False))
    effects = []
    if "tts" in plan["steps"]:
        effects.append("配音按本次确认的方式与项目声源选择处理；不读取句子列表的临时勾选。")
    if "assemble" in plan["steps"]:
        effects.append('生成已保存版本：' + '、'.join(v['name'] for v in versions) + '；不读取成品卡片临时勾选。')
        if auto_comments:
            effects.append('评论准备为自动采集：合成时将自动抓取、评分并翻译上屏评论。')
    result = {**plan, "labels": {k: STEP_MAP[k].label for k in plan["steps"]}, "effects": effects}
    if dubbing is not None:
        result['dubbing'] = dubbing
    from dubforge.workflow_plan import configuration_stamp, digest
    # 配音策略、当前输入和项目状态属于已展示的计划，确认后不得悄悄扩大范围。
    result['fingerprint'] = digest(dict(steps=plan['steps'], configuration=configuration_stamp(deps),
                                      requirements=plan['requirements'], dubbing=dubbing))
    return result



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
    from dubforge.project_archive import assert_active
    project = assert_active(uid)
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
    if body.plan_fingerprint is not None and body.plan_fingerprint != plan['fingerprint']:
        raise DubForgeError('制作方案、配音选择或设置已变化，请重新检查后确认', code='CHAIN_PLAN_CHANGED')
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
    params.pop("_chain_resume_plan", None)
    if plan.get('dubbing'):
        params['_chain_dubbing'] = dict(policy=plan['dubbing']['policy'], operation_id=str(uuid.uuid4()))
    else:
        params.pop('_chain_dubbing', None)
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


@router.post("/{uid}/chain/plan")
def chain_resume_plan(uid: str, body: dict | None = None):
    from dubforge.chain_recovery import plan
    return plan(uid, dubbing=(body or {}).get('dubbing'))


@router.post("/{uid}/chain/resume")
def chain_resume(uid: str, body: dict | None = None):
    """新版确认补齐必要依赖；旧调用只恢复原链，不隐式扩大制作范围。"""
    from dubforge.runner import runner
    from dubforge.pipeline import dubbing_session
    from dubforge import video_info
    from dubforge.publishing import service
    with service._lock, _CHAIN_LOCK, runner._start_lock, dubbing_session.gate, video_info.gate:
        from dubforge.workflows import check_idle
        check_idle(uid)
        from dubforge.project_archive import assert_active
        project = assert_active(uid)
        params = crud.project_params(project)
        checked = bool(body)
        request_id = None
        if checked:
            from dubforge.chain_recovery import ChainRecoveryError
            if body.get('confirmed') is not True:
                raise ChainRecoveryError('请先检查恢复方案并确认', code='CONFIRM_REQUIRED')
            try:
                request_id = str(uuid.UUID(body.get('request_id', '')))
            except (ValueError, TypeError, AttributeError):
                raise ChainRecoveryError('恢复请求标识无效，请重新检查', code='REQUEST_INVALID')
            previous = params.get('_chain_resume_receipts', {}).get(request_id)
            if previous:
                if previous['fingerprint'] != body.get('fingerprint'):
                    raise ChainRecoveryError('此恢复请求已用于另一方案，请重新检查', code='REQUEST_CONFLICT')
                return {**previous['result'], 'reused': True, 'chain': _chain_view(project)}
        if (not isinstance(params.get("_chain"), list) or not params.get("_chain")
                or params.get("_chain_state") != "held"):
            raise DubForgeError("没有挂起的自动运行计划", code="CHAIN_NOT_HELD",
                                suggestion="链在自动运行中或不存在; 可在总览页勾选步骤运行。")
        active = next((s for s in crud.get_steps(project.id)
                       if s.status in {"running", "queued"}), None)
        if active:
            raise DubForgeError("项目已有任务运行中", code="PIPELINE_BUSY",
                                suggestion="等当前步骤结束后再继续剩余计划。")
        dubbing_session.check_idle(uid)
        video_info.check_idle(uid)
        service.check_project_idle(uid)
        if checked:
            from dubforge.chain_recovery import plan, ChainRecoveryError
            candidate = plan(uid, dubbing=body.get('dubbing'))
            if candidate['blockers']:
                raise ChainRecoveryError('暂时无法恢复：' + '；'.join(candidate['blockers']), code='DEPS_NOT_MET')
            if candidate['fingerprint'] != body.get('fingerprint'):
                raise ChainRecoveryError('恢复起点、设置或依赖已变化，请重新检查恢复方案')
            params['_chain'] = candidate['steps']
            params['_chain_resume_plan'] = candidate['execution']
            if candidate.get('dubbing'):
                from dubforge.pipeline.dubbing_policy import current
                previous = params.get('_chain_dubbing') or {}
                policy = candidate['dubbing']['policy']
                params['_chain_dubbing'] = dict(policy=policy,
                    operation_id=previous.get('operation_id') if previous.get('operation_id')
                    and current(previous.get('policy')) == current(policy) else str(uuid.uuid4()))
                params['_chain_resume_plan']['dubbing'] = params['_chain_dubbing']
            # 先持久接收记录，响应丢失或首轮快速失败后同一请求都不会再启动。
            receipts = dict(params.get('_chain_resume_receipts', {}))
            receipts[request_id] = dict(fingerprint=candidate['fingerprint'],
                result=dict(ok=True, started=candidate['recovery']['start_step'], stopped=None))
            params['_chain_resume_receipts'] = receipts
        else:
            params.pop('_chain_resume_plan', None)
        params["_chain_state"] = "active"
        params.pop("_chain_hold", None)
        project.params = json.dumps(params, ensure_ascii=False)
        crud.save_project(project)
        info = runner.continue_chain(project)
        result = {"ok": True, "started": info["started"], "stopped": info["stopped"]}
        if request_id:
            latest = crud.get_project(uid)
            current = crud.project_params(latest)
            current['_chain_resume_receipts'][request_id]['result'] = result
            latest.params = json.dumps(current, ensure_ascii=False)
            crud.save_project(latest)
        return result


@router.post("/{uid}/chain/stop")
def chain_stop(uid: str):
    """明确放弃自动运行计划 (当前正在运行的步骤不受影响, 跑完即止)。"""
    from dubforge.runner import runner
    from dubforge.project_archive import assert_active
    with runner._start_lock:
        project = assert_active(uid)
        runner._clear_chain(project, "用户放弃自动运行计划")
    return {"ok": True}


@router.get("/{uid}/step_params")
def get_project_step_params(uid: str):
    """项目制作基础值与单独设置；defaults 与 parent_sources 表示恢复基础值后的值和来源。"""
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
    reset: list[str] = Field(default_factory=list)     # 移除项目覆盖，恢复项目基础值
    revision: int | None = None
    fingerprint: str | None = None


@router.put("/{uid}/step_params")
def update_project_step_params(uid: str, body: StepParamsUpdate):
    """保存本项目步骤参数；移除覆盖后回到项目基础值，服务连接仍读取最新来源。"""
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
    # 统一配置保存已原子更新项目清单，避免同一次编辑重复写盘。
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


def _workflow_statuses_of(uids):
    """一次读取列表所需的最近任务状态，不按项目另开会话。"""
    if not uids:
        return {}
    from sqlmodel import Session, select
    from dubforge.db import get_engine
    from dubforge.workflow_models import WorkflowRun
    with Session(get_engine()) as session:
        rows = session.exec(select(WorkflowRun.project_uid, WorkflowRun.status)
            .where(WorkflowRun.project_uid.in_(uids))
            .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())).all()
    statuses = {}
    for uid, status in rows:
        statuses.setdefault(uid, status)
    return statuses


def _chain_view(project, *, params=None) -> dict | None:
    """自动续跑链对外视图: 计划与活性分离, held = 挂起可恢复 (ADR-0072)。"""
    params = crud.project_params(project) if params is None else params
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


@router.get('/{uid}/freshness')
def freshness_details(uid: str, response: Response, step: str | None = None):
    """按需查看更新证据；只读取状态，不改配置、不启动制作。"""
    from dubforge.pipeline.dependencies import Dependencies
    project = crud.get_project(uid)
    if not project:
        raise DubForgeError(f'项目不存在: {uid}', code='PROJECT_NOT_FOUND')
    if step is not None and step not in PIPELINE_KEYS:
        raise DubForgeError('请选择有效的制作步骤', code='STEP_NOT_FOUND')
    response.headers['Cache-Control'] = 'no-store'
    deps = Dependencies(project)
    keys = [step] if step is not None else [key for key in PIPELINE_KEYS
        if key in deps.steps and not deps.display_fresh(key)
        and (deps.steps[key].input_fingerprint
             or deps.steps[key].status in {'done', 'stale'})]
    details = []
    for key in keys:
        flags = deps.freshness_flags(key)
        details.append({**deps.explain(key), **flags, 'key': key, 'label': STEP_MAP[key].label,
         'status': deps.status(key),
         **deps.freshness_feedback(key),
         # 本步设置变化不会让后续步骤产生“需更新”标记。
         'affected_steps': [{'key': affected, 'label': STEP_MAP[affected].label}
                            for affected in deps.downstream(key)] if flags['upstream_changed'] else []})
    return {'project_uid': uid, 'steps': details, 'warnings': []}


def _project_view(project, publication=None, release_batch=_RELEASE_UNSET, *, execution_jobs=None,
                  comments_workspace=None, workflow_status=_RELEASE_UNSET, settings_summary=None,
                  project_batch=_RELEASE_UNSET) -> dict:
    # 旧版本或手工 manifest 可能留下未知步骤；项目列表仍应可用，未知行由
    # 重建/迁移流程保留但不让 API 因 STEP_MAP 缺项整体 500。
    from dubforge.pipeline.dependencies import Dependencies, project_status, selected_steps
    deps = Dependencies(project)
    steps = [s for s in deps.steps.values() if s.step_key in STEP_MAP]
    selected = selected_steps(project)
    running = next((s.step_key for s in steps
                    if s.status in ("running", "queued")), None)
    from dubforge.runner import runner
    executions = runner.execution_details(project.id, steps, jobs=execution_jobs)
    if publication is None:
        from dubforge.publishing.service import project_status as publishing_status
        publication = publishing_status(project.uid)
    if release_batch is _RELEASE_UNSET:
        from dubforge import release_batches
        release_batch = release_batches.project_status(project.uid)
    if project_batch is _RELEASE_UNSET:
        from dubforge import project_batches
        project_batch = project_batches.project_statuses([project.uid]).get(project.uid)
    step_views_by_key = {s.step_key: {"key": s.step_key, "label": STEP_MAP[s.step_key].label,
                   "status": deps.status(s.step_key),
                   "progress": 100.0 if deps.status(s.step_key) == "done" else round(s.progress or 0, 1),
                   "selected": s.step_key in selected, "fresh": deps.display_fresh(s.step_key),
                   **deps.freshness_flags(s.step_key),
                   **deps.freshness_feedback(s.step_key),
                   "error": s.error_message if deps.status(s.step_key) != "done" else None,
                   **(runner.progress_eta(project.id, s.step_key)
                      if s.status == "running" else {}),
                   "execution": executions.get(s.step_key),
                   "job_id": runner.current_job_id(project.id, s.step_key)} for s in steps}
    step_views = [step_views_by_key[key] for key in PIPELINE_KEYS
                  if key in step_views_by_key]
    return {
        "uid": project.uid,
        "title": project.title,
        "title_mode": project.title_mode,
        "source_url": project.source_url,
        "source_kind": project.source_kind,
        "status": project_status(project, deps),
        "archived": project.archived_at is not None,
        "archived_at": archived_at_iso(project.archived_at),
        "running_step": running,
        "running_label": STEP_MAP[running].label if running else None,
        "params": crud.project_params(project),
        "chain": _chain_view(project),
        "workflow_status": _workflow_status_of(project.uid) if workflow_status is _RELEASE_UNSET else workflow_status,
        "publication": publication,
        "release_batch": release_batch,
        "project_batch": project_batch,
        **({"project_settings": settings_summary} if settings_summary is not None else {}),
        **({"comments_workspace": comments_workspace} if comments_workspace is not None else {}),
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
