"""项目成片剪辑方案 API。"""
from fastapi import APIRouter, Body, Query, Response

from dubforge.pipeline import output_cuts

router = APIRouter(prefix='/api/projects/{uid}/output-cuts', tags=['output-cuts'])


@router.get('')
def get_output_cuts(uid: str, version_id: str | None = Query(default=None),
                    baseline_version_id: str | None = Query(default=None)):
    """读取指定版本剪辑方案；没有版本号时保持旧项目级方案合同。"""
    return output_cuts.view(uid, version_id=version_id, baseline_version_id=baseline_version_id)


@router.put('')
def save_output_cuts(uid: str, body: dict = Body(...)):
    return output_cuts.save(uid, body)


@router.post('/virtual-preview')
def start_virtual_preview(uid: str, body: dict = Body(...)):
    """按源时间轴拼接当前成片、原视频或其他成片恢复来源的试听任务。

    该任务独立于正式 assemble 步骤，仅生成 artifacts/output_cuts/virtual_preview
    下的临时文件；正式成片仍需另行运行合成步骤。
    """
    from dubforge.pipeline import virtual_preview
    return virtual_preview.start(uid, body)


@router.get('/virtual-preview/context')
def virtual_preview_context(uid: str, response: Response, version_id: str | None = Query(default=None)):
    """返回已生成文件的真实剪辑快照，不以当前草稿或新鲜度代替文件事实。"""
    from dubforge.pipeline import virtual_preview
    response.headers['Cache-Control'] = 'no-store'
    return virtual_preview.context(uid, version_id)


@router.get('/virtual-preview/{task_id}')
def virtual_preview_status(uid: str, task_id: str, response: Response):
    from dubforge.pipeline import virtual_preview
    response.headers['Cache-Control'] = 'no-store'
    return virtual_preview.status(uid, task_id)


@router.post('/virtual-preview/{task_id}/cancel')
@router.delete('/virtual-preview/{task_id}')
def cancel_virtual_preview(uid: str, task_id: str):
    from dubforge.pipeline import virtual_preview
    return virtual_preview.cancel(uid, task_id)


@router.get('/virtual-preview/{task_id}/raw')
@router.head('/virtual-preview/{task_id}/raw', include_in_schema=False)
def virtual_preview_raw(uid: str, task_id: str):
    from app.media import MediaFileResponse
    from dubforge.pipeline import virtual_preview
    return MediaFileResponse(virtual_preview.media_path(uid, task_id), media_type='video/mp4',
                             headers={'Cache-Control': 'no-store'})
