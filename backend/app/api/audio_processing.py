"""逐句后期音量与已生成的声音质量报告；不调用配音模型。"""
from fastapi import APIRouter, Response

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import audio_post, dubbing_session
from dubforge.pipeline.dependencies import Dependencies

router = APIRouter(prefix='/api/projects/{uid}/audio', tags=['audio'])


def _context(uid, line_id):
    project = crud.get_project(uid)
    if project is None:
        raise DubForgeError('项目不存在', code='PROJECT_NOT_FOUND')
    deps = Dependencies(project)
    line = next((row for row in deps.production_lines() if row['id'] == line_id), None)
    if line is None:
        raise DubForgeError('句子已不存在，请刷新列表', code='LINE_NOT_FOUND')
    return project, line, audio_post.audio_version_for_line(line, deps.root, dependencies=deps)


@router.get('/lines/{line_id}')
def get_line_gain(uid: str, line_id: int, response: Response):
    project, line, version = _context(uid, line_id)
    response.headers['Cache-Control'] = 'no-store'
    return audio_post.line_state(project, line, audio_version=version)


@router.put('/lines/{line_id}')
def save_line_gain(uid: str, line_id: int, body: dict):
    project = crud.get_project(uid)
    if project is None:
        raise DubForgeError('项目不存在', code='PROJECT_NOT_FOUND')
    with dubbing_session.edit(project, paths={'assemble.audio_line_gains'}):
        # 门禁内重新取得句子、所选槽与真实文件版本，拒绝迟到的旧表单。
        current, line, version = _context(uid, line_id)
        return audio_post.save_line(current, line, body, audio_version=version)


@router.get('/reports/{artifact_name}')
def get_audio_report(uid: str, artifact_name: str, response: Response):
    project = crud.get_project(uid)
    if project is None:
        raise DubForgeError('项目不存在', code='PROJECT_NOT_FOUND')
    response.headers['Cache-Control'] = 'no-store'
    return dict(report=audio_post.report_view(project, artifact_name))
