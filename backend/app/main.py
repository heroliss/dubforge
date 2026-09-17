# -*- coding: utf-8 -*-
"""FastAPI 应用入口 (壳层; 领域逻辑全部在 dubforge 包内)
- 启动: init_db + 崩溃恢复扫描 (M1 实现)
- 生产: 托管 frontend/dist 静态文件 (存在时), SPA fallback 到 index.html
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dubforge import config
from dubforge.db import init_db
from dubforge.errors import DubForgeError

FRONTEND_DIST = config.REPO_ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # M1: runner.recover_interrupted() —— 启动时把 running 状态的步骤判为 interrupted
    yield


app = FastAPI(title="DubForge", version="0.1.0", lifespan=lifespan)


@app.exception_handler(DubForgeError)
async def dubforge_error_handler(request: Request, exc: DubForgeError):
    """统一业务错误出口: {code, message, suggestion, detail}"""
    return JSONResponse(status_code=400, content=exc.to_dict())


@app.get("/api/health")
def health():
    return {"ok": True, "app": "DubForge", "version": "0.1.0"}


# ---- API 路由 (M1 起逐个模块挂载) ----
from app.api import settings as api_settings  # noqa: E402

app.include_router(api_settings.router)


# ---- 前端静态托管 (frontend/dist 存在时; 开发期走 vite dev server) ----
if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # API 之外的路径全部回退到 index.html (vue-router history 模式)
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
