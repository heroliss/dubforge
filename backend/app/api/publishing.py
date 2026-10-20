"""发布 API；外部副作用必须有明确动作，本地草稿保存不触发网络。"""
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request, Response, UploadFile
from fastapi.responses import FileResponse

from dubforge.errors import DubForgeError
from dubforge.publishing import service, studio, review_draft


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
def publications(project_uid: str | None = None, lifecycle: str = 'active'):
    return service.list_publications(project_uid, lifecycle_filter=lifecycle)


@router.get('/projects/{uid}/candidates')
def candidates(uid: str):
    return service.candidates(uid)


@router.get('/projects/{uid}/status')
def project_status(uid: str):
    return service.project_status(uid)


@router.post('/projects/{uid}/drafts')
def create(uid: str):
    return service.create(uid)


@router.get('/projects/{uid}/copy-preview')
def copy_preview(uid: str):
    return service.copy_preview(uid)


@router.post('/projects/{uid}/suggest')
def suggest_project(uid: str, body: dict):
    return service.suggest_project(uid, body)


@router.post('/projects/{uid}/chapters/translate')
def translate_chapters(uid: str, body: dict):
    from dubforge.publishing import chapters
    project = service._project(uid)
    if not isinstance(body, dict) or 'items' not in body or set(body) - {'items', 'duration', 'translate', 'instruction'}:
        raise service.PublishError('章节整理参数无效', code='PUBLISH_CHAPTER_INVALID')
    # 兼容旧的纯标题调用；有时间轴的候选统一按平台约束整理。
    if set(body) == {'items'} and isinstance(body['items'], list) and body['items'] and all(
            isinstance(row, dict) and 'start_time' not in row for row in body['items']):
        return chapters.translate(body['items'])
    from dubforge import configuration, llm_conversations
    instruction = body.get('instruction', configuration.resolve(uid)['values']['publish.chapters_instruction'])
    with llm_conversations.context(project_uid=uid, operation='chapters', label='整理并审查项目章节'):
        return chapters.adapt(body['items'], duration=body.get('duration'), translate=body.get('translate', True),
                              instruction=instruction, context_title=project.title)


@router.post('/chapters/adapt')
def adapt_chapters(body: dict):
    """只生成本地候选，已有稿件也可使用；不保存草稿或发送平台修改。"""
    from dubforge.publishing.chapters import adapt
    if not isinstance(body, dict) or 'items' not in body or set(body) - {
            'items', 'duration', 'translate', 'instruction', 'project_uid', 'publication_id'}:
        raise service.PublishError('章节整理参数无效', code='PUBLISH_CHAPTER_INVALID')
    uid, owner = body.get('project_uid', ''), body.get('publication_id', '')
    context_title = ''
    if not isinstance(uid, str) or not isinstance(owner, str) or len(uid) > 64 or len(owner) > 64:
        raise service.PublishError('章节对话归属参数无效', code='PUBLISH_CHAPTER_INVALID')
    if owner:
        record = service.view(owner)
        if uid and record['project_uid'] != uid:
            raise service.PublishError('章节候选与来源项目不一致', code='PUBLISH_CHAPTER_INVALID')
        uid = record['project_uid']
        context_title = str(record.get('snapshot', {}).get('settings', {}).get('title', ''))
    if uid:
        project = service._project(uid)
        context_title = context_title or project.title
    from dubforge import llm_conversations
    with llm_conversations.context(project_uid=uid, operation='chapters', label='手动整理并审查视频章节', owner_id=owner):
        return adapt(body['items'], duration=body.get('duration'), translate=body.get('translate', True),
                     instruction=body.get('instruction', ''), context_title=context_title)


@router.get('/publications/{key}')
def view(key: str):
    return service.view(key)


@router.get('/publications/{key}/updates/context')
def update_context(key: str):
    from dubforge.publishing import updates
    return updates.context(key)


@router.post('/publications/{key}/updates/plan')
def update_plan(key: str, body: dict):
    from dubforge.publishing import updates
    return updates.plan(key, body)


@router.get('/updates/{key}')
def update_view(key: str):
    from dubforge.publishing import updates
    return updates.view(key)


@router.post('/updates/{key}/actions')
def update_action(key: str, body: dict):
    from dubforge.publishing import updates
    return updates.action(key, body)


@router.post('/publications/{key}/chapters/retry')
def retry_chapters(key: str, body: dict):
    from dubforge.publishing import chapter_dispatch
    return chapter_dispatch.request(key, body)


@router.put('/publications/{key}')
def save(key: str, body: dict):
    return service.save(key, body)


@router.post('/publications/{key}/archive')
def archive(key: str, body: dict):
    return service.manage_record(key, 'archive', body)


@router.delete('/publications/{key}')
def recycle(key: str, body: dict):
    return service.manage_record(key, 'delete', body)


@router.post('/publications/{key}/restore')
def restore(key: str, body: dict):
    return service.manage_record(key, 'restore', body)


@router.post('/publications/{key}/suggest')
def suggest(key: str, body: dict):
    return service.suggest(key, body)


@router.post('/publications/{key}/copy')
def copy(key: str, body: dict | None = None):
    return service.copy_draft(key, body)


@router.post('/publications/{key}/review-draft/plan')
def review_draft_plan(key: str):
    return review_draft.plan(key)


@router.post('/publications/{key}/review-draft')
def create_review_draft(key: str, body: dict):
    return review_draft.create(key, body)


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


@router.post('/accounts/{account_id}/check')
def check_account(account_id: str):
    return service.check_account(account_id)


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
