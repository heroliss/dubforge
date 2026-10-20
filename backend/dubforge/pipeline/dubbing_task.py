"""配音任务的固定范围和逐句结果；执行、取消与进度统一交给步骤执行器。"""
import hashlib
import json

from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import dubbing_edit


def _confirmation_records(root):
    path = root / 'artifacts' / 'tts' / 'workbench_confirm.json'
    if not path.is_file():
        path = root / 'workbench_confirm.json'
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def confirmation_revision(project, line, root, *, dependencies, selected=None, versions=None,
                          confirms=None, fingerprint=None):
    """只绑定确认窗口实际消费的输入；不解码音频或重新执行整页排布。"""
    from dubforge.pipeline.step_tts import line_fingerprint
    confirms = _confirmation_records(root) if confirms is None else confirms
    selected = selected if selected is not None else dubbing_edit.selection(
        project, line, root, dependencies=dependencies)
    if versions is None:
        versions = [(take, dubbing_edit.file_version(dubbing_edit.take_path(root, line['id'], take)))
                    for take in dubbing_edit.take_ids(root, line['id'])]
    payload = {'input': fingerprint or line_fingerprint(project, line, dependencies=dependencies),
               'selection': selected,
               'takes': [[take, version, confirms.get(dubbing_edit.confirm_key(line['id'], take), {})]
                         for take, version in versions]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def confirmation_slots(project, lines, root, *, dependencies, index=None):
    """槽目录只读文件名和选用记录；未选句的音频不进入提交前关键路径。"""
    index = dubbing_edit.take_index(root) if index is None else index
    params = crud.project_params(project)
    slots = set()
    for line in lines:
        slots.update(index.get(line['id'], []))
        take = dubbing_edit.selection(project, line, root, dependencies=dependencies, params=params)['take']
        if type(take) is int and 1 <= take <= 10000:
            slots.add(take)
    # 已排队的新槽尚无 WAV，也必须参与下一次“新建槽”的编号计算。
    from dubforge.pipeline import live_dubbing
    slots.update(live_dubbing.reserved_takes(project, [line['id'] for line in lines]))
    return sorted(slots)


def _check_confirmation(project, deps, lines, line_ids, expected, *, targets=None,
                        allow_unrelated_slot_changes=False):
    if expected is None:
        return  # 保留旧客户端及单句 API 的调用兼容性。
    if (not isinstance(expected, dict) or set(expected) != {'slots', 'lines'}
            or not isinstance(expected['slots'], list)
            or any(type(take) is not int or not 1 <= take <= 10000 for take in expected['slots'])
            or not isinstance(expected['lines'], dict)
            or set(expected['lines']) != {str(ident) for ident in line_ids}
            or any(not isinstance(value, str) or len(value) != 64 for value in expected['lines'].values())):
        raise DubForgeError('配音确认信息无效，请重新打开确认窗口', code='TTS_CONFIRMATION_INVALID')
    index = dubbing_edit.take_index(deps.root)
    confirms = _confirmation_records(deps.root)
    current_slots = confirmation_slots(project, lines, deps.root, dependencies=deps, index=index)
    changed = current_slots != expected['slots']
    if changed and allow_unrelated_slot_changes:
        # 配音运行中，别句正常完成会扩展目录；只拒绝本次原本空闲的目标槽
        # 已被另一个请求预留。所选句自身的文件/选择仍由 revision 严格校验。
        newly_occupied = set(current_slots) - set(expected['slots'])
        changed = any(row['take'] in newly_occupied for row in (targets or []))
    for line in lines:
        if line['id'] in line_ids and confirmation_revision(
                project, line, deps.root, dependencies=deps, confirms=confirms,
                versions=[(take, dubbing_edit.file_version(dubbing_edit.take_path(deps.root, line['id'], take)))
                          for take in index.get(line['id'], [])]) != expected['lines'][str(line['id'])]:
            changed = True
            break
    if changed:
        raise DubForgeError('句子内容、配音参数或配音槽已变化，请重新确认',
                            code='TTS_CONFIRMATION_CHANGED',
                            suggestion='请刷新配音列表，再重新选择生成范围和目标槽。')


def prepare(project, deps, line_ids, takes=None, expected=None, *, allow_unrelated_slot_changes=False):
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
    selected_lines = [line for line in lines if line['id'] in line_ids]
    targets = []
    reserved = None
    for line in selected_lines:  # 去重并按时间轴执行，不依赖勾选顺序。
        selected = dubbing_edit.selection(project, line, deps.root)['take']
        take = takes.get(str(line['id']), selected)
        if take is None:
            if str(line['id']) in takes:
                if reserved is None:
                    reserved = confirmation_slots(project, selected_lines, deps.root, dependencies=deps)
                take = max(reserved, default=0) + 1
            else:
                # 旧单句接口不指定目标时仍从本句首槽开始；显式 null 才是新槽。
                take = max(dubbing_edit.take_ids(deps.root, line['id']), default=0) + 1
        if type(take) is not int or take < 1:
            raise DubForgeError('配音版本必须为正整数，或选择空位新增', code='DUB_TAKE_INVALID')
        dubbing_edit.take_name(line['id'], take)
        targets.append({'id': line['id'], 'take': take, 'status': 'queued'})
    # runner 的启动锁覆盖验证与任务入队，阻止其它窗口在两者之间改写确认内容。
    _check_confirmation(project, deps, lines, line_ids, expected, targets=targets,
                        allow_unrelated_slot_changes=allow_unrelated_slot_changes)
    for line in lines:
        if line['id'] in line_ids:
            deps.require('workbench', line=line)
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
    project = crud.get_project_by_id(project_id)
    if project is not None:
        from dubforge.pipeline import live_dubbing
        control = live_dubbing._control(project)
        if control.get('job_id') == job.id:
            data['accepted_request_ids'] = list(control.get('accepted_request_ids', []))
            by_id = {row['id']: row for row in rows}
            for ident, pending in control.get('lines', {}).items():
                if pending.get('kind') != 'generation':
                    continue
                row = by_id.get(int(ident))
                if row is None:
                    row = dict(id=int(ident), take=pending.get('target_take'), status='queued')
                    rows.append(row)
                    by_id[int(ident)] = row
                status = pending.get('state', 'queued')
                completed_in_task = (status == 'done' and row.get('live_updated')
                                     and row.get('take') == pending.get('target_take')
                                     and row.get('live_revision') == pending.get('completed_revision'))
                if not completed_in_task:
                    if status == 'done' and pending.get('cached'):
                        status = 'skipped'
                    row.update(status=status, take=pending.get('target_take', row.get('take')),
                               error=pending.get('error'))
                # 候选成功已进入主任务后，以后续排布/缩时的实时状态为准，
                # 不能用先前“自然配音已完成”的回执遮蔽缩时失败或运行中。
                row['request_id'] = pending.get('request_id')
            data['lines'] = rows
    step = crud.get_step(project_id, 'tts')
    queued = job.status == 'running' and step.status == 'queued'
    running = job.status == 'running' and step.status == 'running'
    # ``active`` 保留为整个任务生命周期（含排队）的兼容字段；前端用
    # ``running`` 判断是否需要冻结当前步骤设置。
    active = queued or running
    if not active:
        for row in rows:
            if row['status'] in {'queued', 'running'}:
                row['status'] = 'interrupted'
    return {**data, 'job_id': job.id, 'active': active, 'queued': queued, 'running': running,
            'completed': sum(row['status'] in {'done', 'skipped'} for row in rows),
            'failed': sum(row['status'] == 'failed' for row in rows), 'total': len(rows)}
