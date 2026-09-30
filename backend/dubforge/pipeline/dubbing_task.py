"""配音任务的固定范围和逐句结果；执行、取消与进度统一交给步骤执行器。"""
import json

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_edit


def prepare(project, deps, line_ids, takes=None):
    if (not isinstance(line_ids, list) or not line_ids
            or any(type(i) is not int or i < 1 for i in line_ids)):
        raise DubForgeError('请选择要合成的句子', code='TTS_SELECTION_INVALID')
    if takes is not None and not isinstance(takes, dict):
        raise DubForgeError('配音版本选择格式不正确', code='TTS_SELECTION_INVALID')
    takes = takes or {}
    lines = deps.dubbing_lines(required=True)
    missing = set(line_ids) - {line['id'] for line in lines}
    if missing:
        raise DubForgeError(f'句号不存在: {min(missing)}', code='LINE_NOT_FOUND')
    targets = []
    for line in lines:  # 去重并按时间轴执行，不依赖勾选顺序。
        if line['id'] not in line_ids:
            continue
        deps.require('workbench', line=line)
        selected = dubbing_edit.selection(project, line, deps.root)['take']
        take = takes.get(str(line['id']), selected)
        if take is None:
            take = max(dubbing_edit.take_ids(deps.root, line['id']), default=0) + 1
        if type(take) is not int or take < 1:
            raise DubForgeError('配音版本必须为正整数，或选择空位新增', code='DUB_TAKE_INVALID')
        dubbing_edit.take_name(line['id'], take)
        targets.append({'id': line['id'], 'take': take, 'status': 'queued'})
    return {'scope': 'selected', 'lines': targets}


def view(project_id):
    """重进页面也能看到同一个任务及失败句；不把已结束任务显示成仍在排队。"""
    job = crud.latest_job(project_id, 'tts')
    if job is None:
        return None
    data = json.loads(job.task_data)
    if not data:
        return None
    rows = data.get('lines', [])
    step = crud.get_step(project_id, 'tts')
    active = job.status == 'running' and step.status in {'queued', 'running'}
    if not active:
        for row in rows:
            if row['status'] in {'queued', 'running'}:
                row['status'] = 'interrupted'
    return {**data, 'job_id': job.id, 'active': active,
            'completed': sum(row['status'] in {'done', 'skipped'} for row in rows),
            'failed': sum(row['status'] == 'failed' for row in rows), 'total': len(rows)}
