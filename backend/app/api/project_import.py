"""批量导入 API；文件在浏览器内读成文本，服务端不接受任意清单文件路径。"""
from fastapi import APIRouter, Depends
from app.api.publishing import same_origin
from dubforge import project_import

router = APIRouter(prefix='/api/project-import', tags=['project-import'], dependencies=[Depends(same_origin)])


@router.get('/catalog')
def catalog():
    return project_import.catalog()


@router.get('/history')
def history(limit: int = 20):
    return project_import.history(limit)


@router.post('/preview')
def preview(body: dict):
    return project_import.preview(body)


@router.post('/create')
def create(body: dict):
    return project_import.execute(body)


@router.get('/{request_id}')
def status(request_id: str):
    return project_import.status(request_id)


@router.post('/{request_id}/resume')
def resume(request_id: str, body: dict):
    return project_import.resume(request_id, body)
