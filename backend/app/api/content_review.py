"""项目内容审查HTTP入口。"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from dubforge import content_review_service
from dubforge.content_review_decisions import ContentReviewDecisionError

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


@router.post('/{uid}/content-review/decisions')
def decision(uid: str, body: dict):
    try:
        return content_review_service.save_decision(uid, body)
    except ContentReviewDecisionError as exc:
        if exc.code == 'CONTENT_REVIEW_DECISION_CONFLICT':
            return JSONResponse(status_code=409, content=exc.to_dict())
        raise
