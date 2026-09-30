"""版本 API 只负责协议；继承与渲染规则均在领域入口。"""
from fastapi import APIRouter, Response
from dubforge.pipeline import assembly_versions as versions
from dubforge.runner import runner

router = APIRouter(prefix='/api/projects/{uid}/assembly-versions', tags=['assembly-versions'])


@router.get('')
def view(uid: str, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return versions.view(uid)


@router.put('')
def save(uid: str, body: dict):
    return versions.save(uid, body)


@router.post('/plan')
def plan(uid: str, body: dict):
    return versions.plan(uid, body)


@router.post('/run')
def run(uid: str, body: dict):
    return runner.start_versions(uid, body)


@router.post('/preview')
def preview(uid: str, body: dict):
    return runner.start_versions(uid, body, preview=True)


@router.post('/frame')
def frame(uid: str, body: dict):
    from dubforge.pipeline.version_render import frame
    return Response(frame(uid, body), media_type='image/png', headers={'Cache-Control': 'no-store'})


@router.get('/preview-context')
def preview_context(uid: str):
    from dubforge.pipeline.version_render import preview_context
    return preview_context(uid)
