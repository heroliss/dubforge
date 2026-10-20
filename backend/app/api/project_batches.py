"""混合批量运行接口。"""
from fastapi import APIRouter, Depends, Response

from app.api.publishing import same_origin
from dubforge import project_batches

router = APIRouter(prefix='/api/project-batches', tags=['project-batches'], dependencies=[Depends(same_origin)])


@router.get('/candidates')
def candidates(uids: str, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return project_batches.candidates(uids.split(','))


@router.post('/candidates')
def inspect_candidates(body: dict, response: Response):
    """显式批量检查可补齐来源日期；普通读取、排序与批次恢复不联网采集。"""
    response.headers['Cache-Control'] = 'no-store'
    return project_batches.inspect_candidates(body)


@router.post('/plan')
def plan(body: dict):
    return project_batches.preview(body)


@router.get('')
def listing(response: Response, request_id: str | None = None):
    response.headers['Cache-Control'] = 'no-store'
    return project_batches.listing(request_id)


@router.post('')
def create(body: dict):
    return project_batches.create(body)


@router.get('/{key}')
def view(key: str, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return project_batches.get(key)


@router.post('/{key}/plan')
def recovery_plan(key: str, body: dict | None = None):
    return project_batches.recovery_plan(key, body)


@router.post('/{key}/start')
@router.post('/{key}/resume')
def start(key: str, body: dict):
    return project_batches.start(key, body)


@router.post('/{key}/pause')
def pause(key: str, body: dict | None = None):
    return project_batches.pause(key, body)


@router.post('/{key}/stop')
def stop(key: str, body: dict | None = None):
    return project_batches.stop(key, body)
