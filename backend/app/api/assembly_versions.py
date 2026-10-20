"""版本 API 只负责协议；继承与渲染规则均在领域入口。"""
from fastapi import APIRouter, Depends, Request, Response
from app.api.publishing import same_origin
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


@router.post('/delete-plan', dependencies=[Depends(same_origin)])
def delete_plan(uid: str, body: dict):
    from dubforge.pipeline.assembly_version_deletion import plan
    return plan(uid, body)


@router.post('/delete', dependencies=[Depends(same_origin)])
def delete_version(uid: str, body: dict):
    from dubforge.pipeline.assembly_version_deletion import delete
    return delete(uid, body)


@router.post('/plan')
def plan(uid: str, body: dict):
    return versions.plan(uid, body)


@router.get('/partial-context')
def partial_context(uid: str, response: Response):
    from dubforge.pipeline.assembly_partial import context
    response.headers['Cache-Control'] = 'no-store'
    return context(uid)


@router.get('/sentence-changes')
@router.get('/changes')
def sentence_changes(uid: str, response: Response, version_id: str | None = None):
    from dubforge.pipeline.assembly_sentence_changes import view
    response.headers['Cache-Control'] = 'no-store'
    return view(uid, version_id)


@router.post('/partial-plan')
def partial_plan(uid: str, body: dict):
    from dubforge.pipeline.assembly_partial import plan
    result = plan(uid, body)
    return {key: result[key] for key in ('ranges', 'expanded_ranges', 'duration', 'encode_seconds',
                                        'base_name', 'replace_audio', 'summary', 'fingerprint')}


@router.post('/update-plan')
def update_plan(uid: str, body: dict):
    from dubforge.pipeline.assembly_updates import plan, public
    return public(plan(uid, body))


@router.post('/update-create')
def update_create(uid: str, body: dict):
    from dubforge.pipeline.assembly_updates import create
    return create(uid, body)


@router.post('/partial-create')
def partial_create(uid: str, body: dict):
    from dubforge.pipeline.assembly_partial import create
    return create(uid, body)


@router.post('/timeline/plan')
def timeline_plan(uid: str, body: dict):
    from dubforge.pipeline.assembly_timeline import plan, public
    return public(plan(uid, body))


@router.post('/timeline')
def timeline_create(uid: str, body: dict):
    from dubforge.pipeline.assembly_timeline import create
    return create(uid, body)


@router.get('/defaults-plan')
@router.post('/defaults-plan')
def defaults_plan(uid: str, response: Response):
    from dubforge.pipeline.assembly_inheritance import plan
    response.headers['Cache-Control'] = 'no-store'
    return plan(uid)


@router.post('/apply-defaults')
def apply_defaults(uid: str, body: dict):
    from dubforge.pipeline.assembly_inheritance import apply
    return apply(uid, body)


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


@router.get('/preview-history')
def preview_history(uid: str, response: Response, version_id: str | None = None):
    from dubforge.pipeline.assembly_preview import history
    response.headers['Cache-Control'] = 'no-store'
    return history(uid, version_id)


@router.post('/preview-history/recycle')
def recycle_previews(uid: str, body: dict, request: Request, response: Response):
    from urllib.parse import urlparse
    from dubforge.pipeline.assembly_preview import PreviewError, recycle
    origin = request.headers.get('origin')
    if (request.headers.get('sec-fetch-site') == 'cross-site'
            or origin and urlparse(origin).hostname not in {request.url.hostname, 'localhost', '127.0.0.1', '::1'}):
        raise PreviewError('请在 DubForge 页面内回收试片，不接受跨站请求', code='ASSEMBLY_PREVIEW_ORIGIN')
    response.headers['Cache-Control'] = 'no-store'
    return recycle(uid, body)


@router.get('/preview-history/{name}/raw')
@router.head('/preview-history/{name}/raw', include_in_schema=False)
def preview_history_raw(uid: str, name: str):
    from app.media import MediaFileResponse
    from dubforge.pipeline.assembly_preview import media_path
    return MediaFileResponse(media_path(uid, name), media_type='video/mp4', headers={'Cache-Control': 'no-store'})


@router.get('/preview-history/{name}/audio-report')
def preview_history_audio_report(uid: str, name: str, response: Response):
    from dubforge.pipeline.assembly_preview import audio_report
    response.headers['Cache-Control'] = 'no-store'
    return dict(report=audio_report(uid, name))
