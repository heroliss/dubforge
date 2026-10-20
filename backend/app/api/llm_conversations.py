"""模型对话统一只读入口；响应不缓存，不允许任意路径读取。"""
from fastapi import APIRouter, Query, Response

from dubforge import llm_conversations

router = APIRouter(prefix='/api/llm/conversations', tags=['llm'])


@router.get('')
def conversations(response: Response, project_uid: str = '', operation: str = '', owner_id: str = '',
                  limit: int = Query(default=30, ge=1, le=100), before: str = ''):
    response.headers['Cache-Control'] = 'no-store'
    return llm_conversations.list_conversations(project_uid=project_uid, operation=operation,
                                                owner_id=owner_id, limit=limit, before=before)


@router.get('/{identity}')
def conversation(identity: str, response: Response, after: int = Query(default=0, ge=0),
                 cursor: int | None = Query(default=None, ge=0), trace_id: str = '',
                 limit: int = Query(default=200, ge=1, le=500)):
    response.headers['Cache-Control'] = 'no-store'
    return llm_conversations.read_conversation(identity, after=after if cursor is None else cursor,
                                                trace_id=trace_id, limit=limit)
