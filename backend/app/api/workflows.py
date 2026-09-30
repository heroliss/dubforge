"""全流程 API：预检只读，启动与恢复必须携带精确确认。"""
from fastapi import APIRouter, Depends, Response

from app.api.publishing import same_origin
from dubforge import workflows
from dubforge.workflow_plan import WorkflowError

router = APIRouter(prefix='/api/projects/{uid}/workflow', tags=['workflow'], dependencies=[Depends(same_origin)])


@router.get('')
def view(uid: str, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return workflows.view(uid)


@router.post('/plan')
def plan(uid: str, body: dict):
    return workflows.preview(uid, body)


@router.get('/status')
def status(uid: str, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return workflows.status(uid)


@router.post('/start')
def start(uid: str, body: dict):
    return workflows.start(uid, body)


def _belongs(uid, key):
    if workflows.get(key)['project_uid'] != uid:
        raise WorkflowError('任务不属于当前项目')


@router.post('/{key}/resume')
def resume(uid: str, key: str, body: dict):
    _belongs(uid, key)
    return workflows.resume(key, body)


@router.post('/{key}/stop')
def stop(uid: str, key: str):
    _belongs(uid, key)
    return workflows.stop(key)
