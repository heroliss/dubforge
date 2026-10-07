"""项目内容审查HTTP入口。"""
from fastapi import APIRouter
from dubforge import content_review_service

router = APIRouter(prefix='/api/projects', tags=['content-review'])


@router.get('/{uid}/content-review')
def report(uid: str):
    return content_review_service.report(uid)


@router.post('/{uid}/content-review')
def scan(uid: str):
    return content_review_service.report(uid, persist=True)


@router.put('/{uid}/content-review')
def save(uid: str, body: dict):
    return content_review_service.save(uid, body)
