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
    source_kind: str = "youtube"      # youtube | local
    params: dict = {}


@router.post("")
def create_project(body: ProjectCreate):
    project = crud.create_project(title=body.title, source_url=body.source_url,
                                  source_kind=body.source_kind, params=body.params)
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
