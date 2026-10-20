"""评论精选 API：资料编辑不等于发布；合成需要已确认方案——人工确认，或显式选择自动采集模式 (ADR-0073)。"""
import json
from typing import Literal
from fastapi import APIRouter, Response, Query, Body

from dubforge import comment_studio
from dubforge.domain import crud
from dubforge.errors import DubForgeError

router = APIRouter(prefix='/api/projects/{uid}/comments', tags=['comments'])


@router.get('')
def view(uid: str, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return comment_studio.view(uid)


@router.post('/translate')
def translate(uid: str, body: dict = Body(default={})):
    return comment_studio.run(uid, 'translate', retry_failed=body.get('retry_failed', False), ids=body.get('ids'))


@router.post('/prepare')
def prepare_comments(uid: str, body: dict):
    from dubforge.comment_preparation import prepare
    return prepare(uid, body)


@router.get('/progress')
def progress(uid: str, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return comment_studio.progress(uid)


@router.get('/diagnostics')
def diagnostics(uid: str, response: Response, action: Literal['filter', 'translate'] = 'filter',
                after: int = Query(default=0, ge=0), trace_id: str = ''):
    response.headers['Cache-Control'] = 'no-store'
    return comment_studio.diagnostics(uid, action, after=after, trace_id=trace_id)


@router.post('/filter')
def filter_comments(uid: str, body: dict):
    return comment_studio.run(uid, 'filter', prompt=body.get('prompt'), limit=body.get('limit', 10), force=body.get('force', False),
                             retry_failed=body.get('retry_failed', False), only_cached=body.get('only_cached', False), ids=body.get('ids'))


@router.post('/cancel')
def cancel(uid: str, body: dict):
    return comment_studio.cancel(uid, body.get('job_id'))


@router.put('/overlay')
def save_overlay(uid: str, body: dict):
    return comment_studio.save(uid, body)


@router.post('/preview')
def preview(uid: str, body: dict):
    from dubforge import comment_preview
    from dubforge.pipeline.video_source import resolve_video
    plan = comment_studio.draft(uid, body)
    project = crud.get_project(uid)
    video = resolve_video(project, crud.project_dir_by_project(project))
    if video is None:
        raise DubForgeError('没有可用的源视频', code='VIDEO_MISSING')
    seconds = body.get('seconds', 0)
    # 图像与提示复用同一次画布探测和排版，避免预览后再完整计算一遍。
    frame, details = comment_preview.render(video, plan, seconds)
    headers = {'Cache-Control': 'no-store'}
    if details is not None:
        headers['X-DubForge-Comment-Layout'] = json.dumps(details, separators=(',', ':'))
    return Response(frame, media_type='image/png', headers=headers)
