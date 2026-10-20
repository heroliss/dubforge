"""顺序发布当前阶段的轻量进度：批量读取运行行，不扫描产物或预检项目。"""
import json
import math

from sqlmodel import select, and_, or_

from dubforge.domain.models import Project, StepRun
from dubforge.pipeline.registry import STEP_MAP
from dubforge.publishing.models import Publication
from dubforge.workflow_models import WorkflowRun


ACTIVE = frozenset({'running', 'pausing', 'stopping'})
ITEM_ACTIVE = frozenset({'running', 'monitoring'})


def _object(value, fallback):
    try:
        data = json.loads(value)
        return data if isinstance(data, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


def _number(value, *, percentage=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(max(0, min(100, value)) if percentage else max(0, value), 1)


def _current(row):
    if row.status not in ACTIVE:
        return None
    items = _object(row.items, [])
    if not items:
        return None
    if all(isinstance(item, dict) and item.get('status') == 'done' for item in items):
        return dict(position=None, item=None)
    index = row.cursor if type(row.cursor) is int and 0 <= row.cursor < len(items) else None
    if index is None or not isinstance(items[index], dict) or items[index].get('status') == 'done':
        index = next((i for i, item in enumerate(items) if isinstance(item, dict)
                      and item.get('status') in ITEM_ACTIVE), None)
        if index is None:
            index = next((i for i, item in enumerate(items) if isinstance(item, dict)
                          and item.get('status') != 'done'), None)
    return dict(position=index + 1, item=items[index]) if index is not None else None


def _steps(run):
    values = _object(run.plan, {}).get('steps', []) if run else []
    return values if isinstance(values, list) and all(isinstance(value, str) for value in values) else []


def _base(current):
    item = current['item'] or {}
    result = dict(position=current['position'], project_uid=item.get('project_uid'), title=item.get('title', ''),
                stage='waiting', stage_label='等待开始', phase='等待开始', completed_steps=0,
                total_steps=0, percentage=None, progress_label='等待开始', indeterminate=True,
                eta_seconds=None, estimated=False, execution={})
    if item.get('feedback') == 'preparing':
        result.update(stage='preparing', stage_label='准备执行', phase=item.get('phase') or '正在核对已有结果与执行条件',
                      progress_label=item.get('phase') or '正在核对已有结果与执行条件')
    return result


def _stage(data, stage, label, *, phase=None, progress_label=None):
    return {**data, 'stage': stage, 'stage_label': label, 'phase': phase or label,
            'progress_label': progress_label or phase or label}


def _production(data, run, step, key):
    from dubforge.execution_progress import normalize
    data['execution'] = normalize(_object(getattr(run, 'execution', '{}'), {}), state=run.status,
                                  owner='workflow', run_id=run.id)
    label = STEP_MAP[key].label if key in STEP_MAP else '采集并处理评论' if key == 'comments' else key
    if key == 'tts':
        label = '配音'
    data = _stage(data, 'production', label, phase=run.phase or label)
    if not step or step.status not in {'running', 'done'}:
        phase = '等待任务开始' if step and step.status == 'queued' else data['execution'].get('label') or run.phase or label
        return _stage(data, 'production', label, phase=phase, progress_label=phase)
    live = {}
    if step.status == 'running':
        from dubforge.runner import runner
        try:
            live = runner.progress_eta(step.project_id, key)
        except Exception:
            # 进度读取失败只回退到最近落盘值，不让显示错误中断批次轮询。
            live = {}
    phase = live.get('progress_phase') or data['phase']
    if live.get('execution'):
        data['execution'] = live['execution']
    percent = _number(live.get('progress', step.progress), percentage=True)
    indeterminate = bool(live.get('progress_indeterminate')) or percent is None
    if percent == 0 and not live.get('progress_estimated'):
        # 0也可能只是步骤默认值；尚无真实回调时只显示正在处理。
        percent, indeterminate = None, True
    if step.status == 'done':
        percent, indeterminate = 100.0, False
    elif indeterminate:
        percent = None
    estimated = bool(live.get('progress_estimated')) and percent is not None
    eta = _number(live.get('eta_seconds') or step.eta_seconds)
    suffix = (('预计' if estimated else '') + f'{percent:g}%' if percent is not None else '正在处理')
    return {**data, 'phase': phase, 'percentage': percent, 'indeterminate': indeterminate,
            'eta_seconds': eta if eta and not indeterminate else None, 'estimated': estimated,
            'progress_label': f'{phase} · {suffix}'}


def _publication(data, row, *, reuse=False):
    from dubforge.publishing.service import _execution_view
    data['execution'] = _execution_view(row)
    if row.status in {'published', 'private', 'reviewed'}:
        from dubforge.publishing import chapter_sync
        options = _object(row.snapshot, {}).get('chapter_options') or {}
        chapter = chapter_sync.state_view(row)
        if options.get('enabled') and options.get('items') and chapter.get('status') not in {'completed', 'skipped'}:
            data['execution'] = chapter['execution']
            return _stage(data, 'chapters', '视频章节', phase=chapter.get('phase'),
                          progress_label=chapter.get('error') or chapter.get('phase'))
    if row.status in {'queued', 'uploading'}:
        from dubforge.publishing.service import _progress_detail
        try:
            detail = _progress_detail(row)
        except (ValueError, TypeError, KeyError):
            detail = {}
        phase = detail.get('phase', row.status)
        labels = dict(queued='等待发布开始', validating='发布前核验', preparing='准备上传', uploading='上传视频',
                      merging='合并上传分片', cover='上传封面', uploaded='上传完成')
        label = labels.get(phase, '上传视频')
        data = _stage(data, 'upload' if phase in {'uploading', 'merging'} else 'preparing', label)
        if phase in {'uploading', 'merging'}:
            percent = _number(row.progress, percentage=True)
            part, total = detail.get('current_part', 0), detail.get('total_parts', 0)
            part_label = f' · 第{part}/{total}P' if part and total else ''
            progress = f' · {percent:g}%' if percent is not None else ''
            return {**data, 'percentage': percent, 'indeterminate': percent is None,
                    'progress_label': label + part_label + progress}
        return data
    if row.status == 'submitting':
        return _stage(data, 'submission', '正式投稿', progress_label='请求已发送，等待B站回执')
    if row.status in {'joining', 'collection_pending'}:
        return _stage(data, 'collection', '核对合集归属', progress_label='等待目标合集确认，不能估算百分比')
    if row.status in {'submitted', 'checking'}:
        return _stage(data, 'review', '核对原BV审核' if reuse else '等待B站审核',
                      progress_label='正在核对审核状态' if row.status == 'checking' or reuse else '已投稿，等待B站审核')
    if row.status in {'published', 'private', 'reviewed'} or reuse:
        return _stage(data, 'verifying', '核对原BV与合集' if reuse else '核对合集归属',
                      progress_label='正在核对平台稿件与目标小节')
    if row.status == 'uploaded':
        return _stage(data, 'submission', '准备正式投稿', progress_label='视频已上传，等待正式投稿')
    if row.status in {'unknown', 'rejected', 'failed', 'paused'}:
        return {**_stage(data, 'attention', '等待处理发布结果'), 'indeterminate': False}
    return _stage(data, 'preparing', '准备发布资料')


def load(session, rows):
    """列表与详情共用当前项；每类运行表最多一次查询，历史与待执行项不读取。"""
    current = {row.id: _current(row) for row in rows}
    active = [value['item'] for value in current.values() if value and value['item']
              and value['item'].get('status') in ITEM_ACTIVE and value['item'].get('feedback') != 'preparing']
    workflow_keys = {item.get('workflow_id') for item in active if item.get('workflow_id')
                     and not (item.get('publication') or {}).get('reuse_existing')}
    runs = {run.id: run for run in session.exec(select(WorkflowRun.id, WorkflowRun.project_uid,
        WorkflowRun.status, WorkflowRun.cursor, WorkflowRun.phase, WorkflowRun.publication_id, WorkflowRun.plan,
        WorkflowRun.execution, WorkflowRun.next_check)
        .where(WorkflowRun.id.in_(workflow_keys))).all()} if workflow_keys else {}
    # 项目归属是摘要边界：损坏/旧引用不得带入另一项目的进度或稿件。
    briefs = {key: run for key, run in runs.items()
              if any(item.get('workflow_id') == key and item.get('project_uid') == run.project_uid
                     and (item.get('status') == 'monitoring' or run.status in {'queued', 'running', 'stopping', 'monitoring'})
                     for item in active)}
    expected = []
    publication_keys = set()
    for item in active:
        run = briefs.get(item.get('workflow_id'))
        steps = _steps(run)
        if run and run.status in {'running', 'stopping'} and 0 <= run.cursor < len(steps):
            expected.append(and_(Project.uid == item['project_uid'], StepRun.step_key == steps[run.cursor]))
        elif not run or run.status != 'queued':
            key = run.publication_id if run and run.publication_id else item.get('publication_id')
            if key:
                publication_keys.add(key)
    steps = {(step.project_uid, step.step_key): step for step in session.exec(select(Project.uid.label('project_uid'),
        StepRun.project_id, StepRun.step_key, StepRun.status, StepRun.progress, StepRun.eta_seconds)
        .join(StepRun, StepRun.project_id == Project.id).where(or_(*expected))).all()} if expected else {}
    publications = {row.id: row for row in session.exec(select(Publication.id, Publication.project_uid,
        Publication.status, Publication.progress, Publication.snapshot, Publication.transfers, Publication.chapter_sync)
        .where(Publication.id.in_(publication_keys))).all()} if publication_keys else {}
    result = {}
    for row in rows:
        value = current[row.id]
        if value is None:
            result[row.id] = None
            continue
        data = _base(value)
        item = value['item']
        if item is None:
            result[row.id] = _stage({**data, 'title': '合集顺序'}, 'ordering', '核对并整理合集顺序',
                                   phase=row.phase or '核对并整理合集顺序')
            continue
        if item.get('status') not in ITEM_ACTIVE or item.get('feedback') == 'preparing':
            result[row.id] = data
            continue
        run = briefs.get(item.get('workflow_id'))
        planned = _steps(run)
        completed = max(0, min(len(planned), run.cursor)) if run else 0
        data.update(total_steps=len(planned), completed_steps=completed)
        if run and run.status == 'queued':
            result[row.id] = data
            continue
        if run and run.status in {'running', 'stopping'} and completed < len(planned):
            key = planned[completed]
            data = _production(data, run, steps.get((item['project_uid'], key)), key)
        else:
            key = run.publication_id if run and run.publication_id else item.get('publication_id')
            pub = publications.get(key)
            if pub and pub.project_uid == item['project_uid']:
                data = _publication(data, pub, reuse=bool((item.get('publication') or {}).get('reuse_existing')))
            else:
                data = _stage(data, 'preparing', '准备发布资料', phase=run.phase if run else item.get('phase'))
                if run:
                    from dubforge.execution_progress import normalize
                    data['execution'] = normalize(_object(run.execution, {}), state=run.status, owner='workflow', run_id=run.id)
                    data['phase'] = data['execution'].get('label') or data['phase']
                    data['progress_label'] = data['execution'].get('label') or data['progress_label']
        if row.status == 'stopping':
            data['progress_label'] = '正在停止，等待当前操作收尾 · ' + data['progress_label']
        if run and run.status == 'monitoring' and run.next_check and not data['execution'].get('next_check_at'):
            data['execution'] = {**data['execution'], 'next_check_at': run.next_check}
        result[row.id] = data
    return dict(current=result, briefs=briefs)
