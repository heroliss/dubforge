"""发布 API；外部副作用必须有明确动作，本地草稿保存不触发网络。"""
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request, Response, UploadFile
from fastapi.responses import FileResponse

from dubforge.errors import DubForgeError
from dubforge.publishing import service, studio


def same_origin(request: Request, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    origin = request.headers.get('origin')
    if origin and urlparse(origin).hostname not in {request.url.hostname, 'localhost', '127.0.0.1', '::1'}:
        raise DubForgeError('发布中心不接受来自其他网站的请求', code='PUBLISH_ORIGIN')
    if request.method not in {'GET', 'HEAD'} and request.headers.get('sec-fetch-site') == 'cross-site':
        raise DubForgeError('请在 DubForge 页面中执行发布操作', code='PUBLISH_ORIGIN')


router = APIRouter(prefix='/api/publishing', tags=['publishing'], dependencies=[Depends(same_origin)])


@router.get('/catalog')
def catalog():
    return service.catalog()


@router.get('/publications')
def publications(project_uid: str | None = None):
    return service.list_publications(project_uid)


@router.get('/projects/{uid}/candidates')
def candidates(uid: str):
    return service.candidates(uid)


@router.get('/projects/{uid}/status')
def project_status(uid: str):
    return service.project_status(uid)


@router.post('/projects/{uid}/drafts')
def create(uid: str):
    return service.create(uid)


@router.get('/publications/{key}')
def view(key: str):
    return service.view(key)


@router.put('/publications/{key}')
def save(key: str, body: dict):
    return service.save(key, body)


@router.post('/publications/{key}/suggest')
def suggest(key: str, body: dict):
    return service.suggest(key, body)


@router.post('/publications/{key}/copy')
def copy(key: str):
    return service.copy_draft(key)


@router.post('/publications/{key}/plan')
def plan(key: str):
    return service.plan(key)


@router.post('/publications/{key}/actions')
def action(key: str, body: dict):
    return service.enqueue(key, body)


@router.post('/publications/{key}/pause')
def pause(key: str):
    return service.pause(key)


@router.post('/publications/{key}/receipt')
def receipt(key: str, body: dict):
    return service.attach_receipt(key, body)


@router.post('/login')
def login():
    return service.login_start()


@router.get('/login/{key}')
def login_status(key: str):
    return service.login_view(key)


@router.post('/login/{key}/cancel')
def login_cancel(key: str):
    return service.login_view(key, cancel=True)


@router.post('/accounts/{account_id}/metadata')
def metadata(account_id: str):
    return service.metadata(account_id)


@router.post('/accounts/{account_id}/studio/read/{action}')
def studio_read(account_id: str, action: str, body: dict):
    return studio.read(account_id, action, body)


@router.get('/accounts/{account_id}/studio/changes')
def studio_changes(account_id: str):
    return studio.changes(account_id)


@router.post('/accounts/{account_id}/studio/changes')
def studio_prepare(account_id: str, body: dict):
    return studio.prepare(account_id, body)


@router.post('/accounts/{account_id}/studio/changes/{key}/commit')
def studio_commit(account_id: str, key: str, body: dict):
    return studio.commit(account_id, key, body)


@router.post('/accounts/{account_id}/studio/changes/{key}/reconcile')
def studio_reconcile(account_id: str, key: str, body: dict):
    return studio.reconcile(account_id, key, body)


@router.post('/accounts/{account_id}/studio/import-template')
def studio_import_template(account_id: str, body: dict):
    return studio.import_template(account_id, body)


@router.post('/accounts/{account_id}/studio/import-content')
def studio_import_content(account_id: str, body: dict):
    from dubforge.publishing.imports import apply
    return apply(account_id, body)


@router.post('/accounts/{account_id}/collections')
def create_collection(account_id: str, body: dict):
    return service.create_collection(account_id, body)


@router.get('/accounts/{account_id}/collection-requests')
def pending_collections(account_id: str):
    return service.pending_collections(account_id)


@router.post('/accounts/{account_id}/collection-requests/{key}/resolve')
def resolve_collection(account_id: str, key: str, body: dict):
    return service.resolve_collection(account_id, key, body)


@router.post('/covers')
async def cover(file: UploadFile):
    return service.store_cover(await file.read(10 * 1024 * 1024 + 1))


@router.get('/covers/{key}')
def cover_image(key: str):
    return FileResponse(service._cover_path(key), headers={'Cache-Control': 'private, no-store'})


@router.post('/projects/{uid}/platform-cover')
def platform_cover(uid: str):
    return service.platform_cover(uid)
