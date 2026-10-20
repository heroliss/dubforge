"""统一配置 API；运行检查不另存配方。"""
from fastapi import APIRouter, Depends
from app.api.publishing import same_origin
from dubforge import configuration, configuration_trash

router = APIRouter(prefix='/api/configuration', tags=['configuration'], dependencies=[Depends(same_origin)])


@router.get('/templates')
def templates():
    return configuration.list_templates()


@router.post('/templates')
def create(body: dict):
    return configuration.create_template(body.get('name'))


@router.get('/templates/trash')
def deleted_templates():
    return configuration_trash.list_deleted()


@router.post('/templates/trash/{backup_id}/restore')
def restore_template(backup_id: str, body: dict):
    return configuration_trash.restore(backup_id, body)


@router.post('/binding-plan')
def binding_plan(body: dict):
    return configuration.binding_plan(body.get('uids'), body.get('template_id', ''))


@router.post('/bind')
def bind(body: dict):
    return configuration.bind(body)


@router.get('/{key}')
def view(key: str):
    return configuration.view(key)


@router.get('/{key}/editability')
def editability(key: str):
    from dubforge import runtime_edit
    return runtime_edit.editability(key)


@router.post('/batch-plan')
def batch_plan(body: dict):
    return configuration.batch_plan(body)


@router.post('/batch-start')
def batch_start(body: dict):
    return configuration.batch_start(body)


@router.post('/{key}/archive')
def archive(key: str, body: dict):
    return configuration.archive(key, body)


@router.put('/{key}')
def save(key: str, body: dict):
    return configuration.save(key, body)


@router.delete('/{key}')
def delete_template(key: str, body: dict):
    return configuration_trash.delete(key, body)
