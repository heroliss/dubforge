"""批次预检只读；创建不执行，开始/恢复须本次精确确认。"""
from fastapi import APIRouter, Depends, Response

from app.api.publishing import same_origin
from dubforge import release_batches

router = APIRouter(prefix='/api/release-batches', tags=['release-batches'], dependencies=[Depends(same_origin)])


@router.get('/candidates')
def candidates(uids: str, response: Response, target_driven: bool = False):
    response.headers['Cache-Control'] = 'no-store'
    if len(uids) > 5050:
        raise release_batches.ReleaseBatchError('所选项目过多，请分为不超过50项的批次')
    return release_batches.candidates(uids.split(','), target_driven=target_driven)


@router.post('/plan')
def plan(body: dict):
    """版本选择及未投稿草稿更新均进入确认指纹；预检不要求已存在新成片。"""
    return release_batches.preview(body)


@router.get('')
def listing(response: Response, request_id: str | None = None):
    response.headers['Cache-Control'] = 'no-store'
    return release_batches.listing(request_id)


@router.post('')
def create(body: dict):
    return release_batches.create(body)


@router.get('/{key}')
def view(key: str, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return release_batches.get(key)


@router.post('/{key}/plan')
def recovery_plan(key: str, body: dict | None = None):
    """恢复预检；允许用户明确把退回/不明原稿换绑到现有未投稿草稿。"""
    body = body or {}
    return release_batches.recovery_plan(key, publication_ids=body.get('publication_ids'))


@router.post('/{key}/start')
@router.post('/{key}/resume')
def start(key: str, body: dict):
    return release_batches.start(key, body)


@router.post('/{key}/pause')
def pause(key: str):
    return release_batches.pause(key)


@router.post('/{key}/stop')
def stop(key: str):
    return release_batches.stop(key)
