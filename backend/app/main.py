"""FastAPI 应用入口 (壳层; 领域逻辑全部在 dubforge 包内)
- 启动: init_db + 崩溃恢复扫描 (M1 实现)
- 生产: 托管 frontend/dist 静态文件 (存在时), SPA fallback 到 index.html
"""
import logging
from contextlib import asynccontextmanager

from dubforge import config
from dubforge.db import init_db
from dubforge.errors import DubForgeError
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

FRONTEND_DIST = config.REPO_ROOT / "frontend" / "dist"
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from dubforge.domain import crud
    # 目录+manifest 是事实, DB 是可重建索引: 只补缺 (DB 正常时零操作),
    # 同时把手动拷进 projects/ 的项目文件夹收编进索引 (拷文件夹 = 搬项目)
    rebuilt = crud.rebuild_from_dirs()
    if rebuilt["projects"]:
        logger.warning("从项目目录收编了 %d 个未登记的项目 (步骤 %d 行 / 产物 %d 条)",
                       rebuilt["projects"], rebuilt["steps"], rebuilt["artifacts"])
    added = crud.backfill_steps()
    if added:
        logger.warning("为老项目补齐了 %d 个缺失的步骤行", added)
    repaired = crud.repair_unstarted_stale_steps()
    if repaired:
        logger.info("已将 %d 个从未运行却被标记过期的步骤恢复为待运行", repaired)
    # 崩溃恢复: 上次运行残留的 running 状态判中断 (原子写保证无半成品)
    from dubforge.runner import runner
    runner.recover()
    from dubforge.pipeline.config_fingerprint import promote_compatible_fingerprints
    promoted = promote_compatible_fingerprints()
    if promoted:
        logger.info("已兼容迁移 %d 个制作指纹；连接凭据和超时不再使文字结果过期", promoted)
    from dubforge.publishing import service as publishing
    publishing.recover()
    from dubforge import workflows
    workflows.recover()
    workflows.start_monitor()
    try:
        yield
    finally:
        workflows.shutdown()
        publishing.interrupt_all()


app = FastAPI(title="DubForge", version="0.1.0", lifespan=lifespan)


@app.exception_handler(DubForgeError)
async def dubforge_error_handler(request: Request, exc: DubForgeError):
    """统一业务错误出口: {code, message, suggestion, detail}"""
    return JSONResponse(status_code=400, content=exc.to_dict())


@app.get("/api/health")
def health():
    return {"ok": True, "app": "DubForge", "version": "0.1.0"}


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
app.include_router(api_configuration.router)
app.include_router(api_project_import.router)
from app.api import content_review as api_content_review
app.include_router(api_content_review.router)


# ---- 前端静态托管 (frontend/dist 存在时; 开发期走 vite dev server) ----
if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
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
