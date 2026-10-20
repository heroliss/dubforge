"""核对短片的生成与 Range 媒体响应；编码实现在领域层。"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.media import MediaFileResponse
from dubforge import content_review_preview

router = APIRouter(prefix='/api/projects', tags=['content-review'])


@router.post('/{uid}/content-review/preview')
def preview(uid: str, body: dict):
    try:
        return content_review_preview.generate(uid, body)
    except content_review_preview.ReviewPreviewError as exc:
        status = {'CONTENT_REVIEW_PREVIEW_CONFLICT': 409, 'CONTENT_REVIEW_PREVIEW_BUSY': 429}.get(exc.code, 400)
        return JSONResponse(status_code=status, content=exc.to_dict())


@router.get('/{uid}/content-review/preview/{fingerprint}/{mode}')
@router.head('/{uid}/content-review/preview/{fingerprint}/{mode}', include_in_schema=False)
def media(uid: str, fingerprint: str, mode: str):
    path = content_review_preview.media(uid, fingerprint, mode)
    return MediaFileResponse(path, media_type='video/mp4', headers={'Cache-Control': 'no-store'})
