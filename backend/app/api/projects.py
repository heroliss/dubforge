# -*- coding: utf-8 -*-
"""项目 API: CRUD (M1; M4 前端接手)"""
import json

from fastapi import APIRouter
from pydantic import BaseModel

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline.registry import STEP_MAP

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


@router.delete("/{uid}")
def delete_project(uid: str):
    if not crud.delete_project(uid):
        raise DubForgeError(f"项目不存在: {uid}", code="PROJECT_NOT_FOUND")
    return {"ok": True}


def _project_view(project) -> dict:
    return {
        "uid": project.uid,
        "title": project.title,
        "source_url": project.source_url,
        "source_kind": project.source_kind,
        "status": project.status,
        "params": json.loads(project.params or "{}"),
        "created_at": project.created_at.isoformat(),
    }
