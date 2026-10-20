"""FastAPI 应用入口 (壳层; 领域逻辑全部在 dubforge 包内)
- 启动: init_db + 崩溃恢复扫描 (M1 实现)
- 生产: 托管 frontend/dist 静态文件 (存在时), SPA fallback 到 index.html
"""
import logging
import time
from contextlib import asynccontextmanager

from dubforge import __version__, config
from dubforge.db import init_db
from dubforge.errors import DubForgeError
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.routing import Match

FRONTEND_DIST = config.REPO_ROOT / "frontend" / "dist"
logger = logging.getLogger(__name__)
startup_logger = logging.getLogger("uvicorn.error")


def _startup_step(name, action):
    """逐阶段记录耗时，区分仍在初始化与初始化失败。"""
    started = time.monotonic()
    startup_logger.info("启动初始化：%s", name)
    try:
        result = action()
    except Exception:
        startup_logger.exception("启动初始化失败：%s（%.2f 秒）", name, time.monotonic() - started)
        raise
    startup_logger.info("启动初始化完成：%s（%.2f 秒）", name, time.monotonic() - started)
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    started = time.monotonic()
    _startup_step("数据库与配置迁移", init_db)
    from dubforge.domain import crud
    # 目录+manifest 是事实, DB 是可重建索引: 只补缺 (DB 正常时零操作),
    # 同时把手动拷进 projects/ 的项目文件夹收编进索引 (拷文件夹 = 搬项目)
    rebuilt = _startup_step("项目目录索引", crud.rebuild_from_dirs)
    if rebuilt["projects"]:
        logger.warning("从项目目录收编了 %d 个未登记的项目 (步骤 %d 行 / 产物 %d 条)",
                       rebuilt["projects"], rebuilt["steps"], rebuilt["artifacts"])
    added = _startup_step("补齐项目步骤", crud.backfill_steps)
    if added:
        logger.warning("为老项目补齐了 %d 个缺失的步骤行", added)
    repaired = _startup_step("修复待运行步骤", crud.repair_unstarted_stale_steps)
    if repaired:
        logger.info("已将 %d 个从未运行却被标记过期的步骤恢复为待运行", repaired)
    # 崩溃恢复: 上次运行残留的 running 状态判中断 (原子写保证无半成品)
    from dubforge.runner import runner
    from dubforge import comment_studio
    _startup_step("准备制作执行器", runner.startup)
    _startup_step("准备评论执行器", comment_studio.startup)
    _startup_step("恢复制作任务", runner.recover)
    from dubforge.pipeline.config_fingerprint import promote_compatible_fingerprints
    promoted = _startup_step("兼容旧产物指纹", promote_compatible_fingerprints)
    if promoted:
        logger.info("已兼容迁移 %d 个制作指纹；模型与服务设置变更不再使已有结果过期", promoted)
    from dubforge.publishing import service as publishing
    _startup_step("恢复发布记录", publishing.recover)
    from dubforge.publishing import updates as publication_updates
    _startup_step("恢复稿件更新", publication_updates.startup)
    from dubforge import workflows
    _startup_step("恢复项目流程", workflows.recover)
    from dubforge import release_batches
    _startup_step("恢复发布批次", release_batches.recover)
    from dubforge import project_batches
    _startup_step("恢复混合项目批次", project_batches.recover)
    from dubforge.publishing import chapter_dispatch
    _startup_step("恢复章节任务", chapter_dispatch.startup)
    _startup_step("启动流程监控", workflows.start_monitor)
    startup_logger.info("后端初始化完成，总耗时 %.2f 秒", time.monotonic() - started)
    try:
        yield
    finally:
        # 先关闭后台任务入口并发送取消，避免主线程退出后下一批仍尝试网络请求。
        runner.request_shutdown()
        comment_studio.request_shutdown()
        deadline = time.monotonic() + 5
        project_batches.shutdown()
        release_batches.shutdown()
        workflows.shutdown()
        chapter_dispatch.shutdown()
        publication_updates.shutdown()
        publishing.interrupt_all()
        pending_steps = runner.wait_for_shutdown(max(0.0, deadline - time.monotonic()))
        pending_comments = comment_studio.wait_for_shutdown(max(0.0, deadline - time.monotonic()))
        if pending_steps or pending_comments:
            logger.warning('关闭等待已结束，%d 个步骤、%d 个评论任务仍在取消收尾；重启后可继续',
                           pending_steps, pending_comments)


app = FastAPI(title="DubForge", version=__version__, lifespan=lifespan)


def _is_api_path(path: str):
    path = path.lstrip('/')
    return path == 'api' or path.startswith('api/')


class _SPARoute(APIRoute):
    """页面兜底不参与 API 匹配，保留真实接口的 405 与 Allow。"""

    def matches(self, scope):
        match, child_scope = super().matches(scope)
        if _is_api_path(child_scope.get('path_params', {}).get('full_path', '')):
            return Match.NONE, {}
        return match, child_scope


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    # API 404 与是否构建网页无关；其他 HTTP 错误沿用框架响应及原有头部。
    path = request.scope['path']
    root_path = request.scope.get('root_path', '')
    if root_path and (path == root_path or path.startswith(root_path + '/')):
        path = path[len(root_path):]
    if exc.status_code == 404 and _is_api_path(path):
        return JSONResponse(status_code=404, content={
            'code': 'API_NOT_FOUND', 'message': '请求的接口不存在，请刷新页面后重试',
        }, headers={**(exc.headers or {}), 'Cache-Control': 'no-store'})
    return await http_exception_handler(request, exc)


@app.exception_handler(DubForgeError)
async def dubforge_error_handler(request: Request, exc: DubForgeError):
    """统一业务错误出口: {code, message, suggestion, detail}"""
    return JSONResponse(status_code=400, content=exc.to_dict())


@app.get("/api/health")
async def health():
    # 健康探针必须绕过 AnyIO 的同步线程池；步骤状态或磁盘扫描拥塞时，
    # 启动器仍需能判断服务进程是否存活并给出可操作提示。
    return {"ok": True, "app": "DubForge", "version": __version__}


# ---- API 路由 ----
from app.api import files as api_files
from app.api import services as api_services
from app.api import presets as api_presets
from app.api import projects as api_projects
from app.api import settings as api_settings
from app.api import steps as api_steps
from app.api import workbench as api_workbench
from app.api import comments as api_comments
from app.api import publishing as api_publishing
from app.api import workflows as api_workflows
from app.api import assembly_versions as api_assembly_versions
from app.api import configuration as api_configuration
from app.api import project_import as api_project_import
from app.api import release_batches as api_release_batches
from app.api import project_batches as api_project_batches
from app.api import llm_conversations as api_llm_conversations

app.include_router(api_settings.router)
app.include_router(api_projects.router)
app.include_router(api_steps.router)
app.include_router(api_workbench.router)
app.include_router(api_presets.router)
app.include_router(api_files.router)
app.include_router(api_services.router)
app.include_router(api_comments.router)
app.include_router(api_publishing.router)
app.include_router(api_workflows.router)
app.include_router(api_assembly_versions.router)
from app.api import output_cuts as api_output_cuts
app.include_router(api_output_cuts.router)
app.include_router(api_configuration.router)
app.include_router(api_project_import.router)
app.include_router(api_release_batches.router)
app.include_router(api_project_batches.router)
app.include_router(api_llm_conversations.router)
from app.api import content_review as api_content_review
app.include_router(api_content_review.router)
from app.api import content_review_preview as api_content_review_preview
app.include_router(api_content_review_preview.router)
from app.api import audio_processing as api_audio_processing
app.include_router(api_audio_processing.router)


# ---- 前端静态托管 (frontend/dist 存在时; 开发期走 vite dev server) ----
if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    def spa(full_path: str):
        # API 之外的路径全部回退到 index.html; index 禁缓存 (防旧资源清单)。
        # 候选路径必须仍在 dist 内 (resolve 后校验) —— 挡住 /..%2F..%2Fconfig.yaml
        # 这类路径穿越, 防止把仓库里的密钥文件当静态资源吐出去。
        headers = {"Cache-Control": "no-store"}
        if full_path:
            candidate = (FRONTEND_DIST / full_path).resolve()
            if candidate.is_relative_to(FRONTEND_DIST) and candidate.is_file():
                return FileResponse(candidate, headers=headers)
        return FileResponse(FRONTEND_DIST / "index.html", headers=headers)

    app.router.add_api_route("/{full_path:path}", spa, methods=['GET'], route_class_override=_SPARoute)
