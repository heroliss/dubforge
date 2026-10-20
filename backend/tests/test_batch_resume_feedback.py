"""恢复受理与真实步骤分开反馈；全部数据隔离，不启动模型或平台任务。"""
import copy
import json
import threading
import uuid

import pytest
from sqlalchemy import event
from sqlmodel import Session

from dubforge import project_batches, release_batches, workflows
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.project_batch_models import ProjectBatch
from dubforge.release_batch_models import ReleaseBatch
from dubforge.workflow_models import WorkflowRun


@pytest.fixture
def receipt(make_project, monkeypatch):
    uids = [make_project(title=f'恢复项目{i}') for i in range(3)]
    groups = [dict(id=uuid.uuid4().hex, kind='collection', title='合集组',
        scope=dict(account_id='123', season_id=1, section_id=2), project_uids=uids[:2],
        child_id=uuid.uuid4().hex, workflow_id='', status='attention', phase='上次语义整理失败',
        error='旧模型错误', blockers=[], warnings=[], plan=dict(fingerprint='confirmed-child-plan'),
        items=[dict(project_uid=uids[0], title='已完成项目', status='done', phase='已完成', error=''),
               dict(project_uid=uids[1], title='待恢复项目', status='attention', phase='上次语义整理失败',
                    error='旧模型错误', workflow_id='old-workflow', failed_step='split',
                    error_code='LLM_RATE_LIMITED', failure={'code': 'LLM_RATE_LIMITED'})]),
        dict(id=uuid.uuid4().hex, kind='independent', title='其他组', project_uids=[uids[2]],
             status='attention', phase='其他组保持原样', error='另一项错误', blockers=[], warnings=[],
             plan=dict(fingerprint='other-plan'),
             items=[dict(project_uid=uids[2], status='failed', phase='其他组旧失败')])]
    with Session(get_engine()) as session:
        row = ProjectBatch(request_id=str(uuid.uuid4()), status='attention', groups=json.dumps(groups),
                           error='；'.join(group['error'] for group in groups))
        session.add(row)
        session.commit()
        session.refresh(row)
        key = row.id
    for uid in uids:
        step = crud.get_step(crud.get_project(uid).id, 'split')
        step.status, step.error_message = 'failed', '真实历史步骤失败'
        crud.save_step(step)
    for module in (project_batches, release_batches, workflows):
        monkeypatch.setattr(module, '_leases', {})
        monkeypatch.setattr(module, '_running', {})
        monkeypatch.setattr(module, '_owner', threading.local())
        if module is not workflows:
            monkeypatch.setattr(module, '_closing', False)
    monkeypatch.setattr(project_batches, '_children', {})
    monkeypatch.setattr(release_batches, '_children', {})
    monkeypatch.setattr(project_batches, '_conflict', lambda *_args: None)
    return dict(id=key, groups=groups, uids=uids)


def test_accepted_selected_group_has_preparation_feedback_before_worker_starts(receipt, monkeypatch):
    env = receipt
    selected = env['groups'][0]['id']
    assert project_batches.get(env['id'])['error'] == '旧模型错误；另一项错误'
    candidate = project_batches._result(copy.deepcopy(env['groups']), selected=[selected])
    monkeypatch.setattr(project_batches, 'recovery_plan', lambda *_args, **_kwargs: copy.deepcopy(candidate))
    pending = []
    class DeferredThread:
        def __init__(self, *, target, args, **_kwargs):
            pending.append((target, args))
        def start(self):
            pass
    monkeypatch.setattr(project_batches.threading, 'Thread', DeferredThread)
    body = dict(request_id=str(uuid.uuid4()), confirmed=True, rights_confirmed=True,
                group_ids=[selected], fingerprint=candidate['fingerprint'])
    row = project_batches.start(env['id'], body)
    group = row['groups'][0]
    assert row['status'] == group['status'] == 'running'
    assert row['error'] == '另一项错误', '父聚合错误必须同步剔除已受理组的历史错误'
    assert row['phase'] == '已完成 0/2 组；正在准备继续 1 组'
    assert group['feedback'] == 'preparing' and group['error'] == ''
    assert group['items'][0] == env['groups'][0]['items'][0], '已完成结果不能回退到准备状态'
    item = group['items'][1]
    assert item['status'] == 'queued' and item['feedback'] == 'preparing' and item['error'] == ''
    assert 'failed_step' not in item and 'error_code' not in item and 'failure' not in item
    assert '核对已有结果' in item['phase']
    assert row['groups'][1] == env['groups'][1], '只恢复本组不能改变另一组'
    summary = project_batches.listing()['batches'][0]['groups'][0]
    assert summary['feedback'] == summary['items'][1]['feedback'] == 'preparing'
    assert project_batches.listing()['batches'][0]['error'] == '另一项错误'
    status = project_batches.project_statuses([env['uids'][1]])[env['uids'][1]]
    assert status['item_status'] == 'queued' and status['feedback'] == 'preparing'
    assert status['item_phase'] == item['phase']
    assert crud.get_step(crud.get_project(env['uids'][1]).id, 'split').status == 'failed'
    assert crud.latest_job(crud.get_project(env['uids'][1]).id, 'split') is None
    assert len(pending) == 1
    assert project_batches.start(env['id'], body)['groups'][0]['feedback'] == 'preparing'
    assert len(pending) == 1, '同一受理回执重试不能启动第二个线程'


@pytest.mark.parametrize('status', ['attention', 'paused', 'stopped'])
def test_preparation_feedback_ends_when_group_stops_or_validation_fails(receipt, status):
    env = receipt
    group = copy.deepcopy(env['groups'][0])
    for item in group['items']:
        if item['status'] != 'done':
            item.update(status='queued', feedback='preparing', phase='正在准备')
    project_batches._save_group(env['id'], group['id'], status='running', feedback='preparing', items=group['items'])
    project_batches._save_group(env['id'], group['id'], status=status, phase='检查未通过', error='稿件回执需要核对')
    current = project_batches.get(env['id'])['groups'][0]
    assert current['feedback'] == ''
    assert all(item.get('feedback') != 'preparing' for item in current['items'])
    assert current['error'] == '稿件回执需要核对'
    assert current['items'][0]['status'] == 'done'


def test_restart_clears_parent_and_item_preparation_feedback(receipt):
    env = receipt
    group = copy.deepcopy(env['groups'][0])
    group['items'][1].update(status='queued', feedback='preparing', phase='正在准备')
    project_batches._save_group(env['id'], group['id'], status='running', feedback='preparing', items=group['items'])
    project_batches.recover()
    current = project_batches.get(env['id'])['groups'][0]
    assert current['status'] == 'paused' and current['feedback'] == ''
    assert current['items'][1]['feedback'] == ''
    assert '准备' not in current['items'][1]['phase']
    assert current['items'][0]['status'] == 'done'


@pytest.mark.parametrize('action', ['restart', 'pause_before_worker', 'stopped', 'attention'])
def test_release_preparation_feedback_ends_on_all_inactive_paths(make_project, monkeypatch, action):
    uid = make_project()
    with Session(get_engine()) as session:
        row = ReleaseBatch(request_id=str(uuid.uuid4()), status='running',
            items=json.dumps([dict(project_uid=uid, title='准备恢复', status='queued', feedback='preparing', phase='正在准备')]))
        session.add(row)
        session.commit()
        session.refresh(row)
        key = row.id
    monkeypatch.setattr(release_batches, '_closing', False)
    monkeypatch.setattr(release_batches, '_running', {})
    monkeypatch.setattr(release_batches, '_leases', {})
    monkeypatch.setattr(release_batches, '_children', {})
    if action == 'restart':
        release_batches.recover()
    elif action == 'pause_before_worker':
        release_batches.pause(key)
        release_batches._execute(key, threading.Event())
    else:
        release_batches._update(key, status=action)
    value = release_batches.get(key)
    assert value['status'] in {'paused', 'stopped', 'attention'}
    assert value['items'][0]['feedback'] == ''
    assert value['current_item'] is None


def test_collection_parent_defers_final_online_check_to_child_once(receipt, monkeypatch):
    env = receipt
    group = env['groups'][0]
    starts = []
    def child(key):
        return dict(id=key, status='done' if starts else 'attention', phase='已完成' if starts else '待处理',
                    error='', items=copy.deepcopy(group['items']))
    def start(key, body):
        assert body['fingerprint'] == group['plan']['fingerprint']
        assert body['confirmed'] and body['rights_confirmed']
        starts.append((key, body))
        return child(key)
    def forbidden(*_args, **_kwargs):
        pytest.fail('父执行器不能在 child.start 的最终检查前重复在线恢复检查')
    monkeypatch.setattr(release_batches, 'get', child)
    monkeypatch.setattr(release_batches, 'start', start)
    monkeypatch.setattr(release_batches, 'recovery_plan', forbidden)
    project_batches._save_group(env['id'], group['id'], status='running', feedback='preparing')
    project_batches._running[(env['id'], group['id'])] = threading.Event()
    for uid in group['project_uids']:
        project_batches._leases[uid] = env['id']
    project_batches._execute(env['id'], group['id'], str(uuid.uuid4()), threading.Event())
    assert len(starts) == 1
    current = project_batches.get(env['id'])['groups'][0]
    assert current['status'] == 'done' and current['feedback'] == ''


def test_child_final_online_change_still_blocks_parent_before_execution(receipt, monkeypatch):
    env = receipt
    group = env['groups'][0]
    with Session(get_engine()) as session:
        session.add(ReleaseBatch(id=group['child_id'], request_id=str(uuid.uuid4()), status='attention',
            plan=json.dumps(dict(parent_batch_id=env['id'])), items='[]'))
        session.commit()
    checked = []
    def changed(_key, **kwargs):
        checked.append(kwargs.get('online', True))
        return dict(fingerprint='changed-after-confirmation', blockers=[])
    monkeypatch.setattr(release_batches, 'recovery_plan', changed)
    project_batches._save_group(env['id'], group['id'], status='running', feedback='preparing')
    project_batches._running[(env['id'], group['id'])] = threading.Event()
    for uid in group['project_uids']:
        project_batches._leases[uid] = env['id']
    project_batches._execute(env['id'], group['id'], str(uuid.uuid4()), threading.Event())
    assert checked == [True], '真正子执行器的最终在线核验必须保留一次'
    current = project_batches.get(env['id'])['groups'][0]
    assert current['status'] == 'attention' and current['feedback'] == ''
    assert '预检已变化' in current['error']
    assert group['child_id'] not in release_batches._running
    assert all(crud.latest_job(crud.get_project(uid).id, 'split') is None for uid in group['project_uids'])


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_preparing_release_projection_ignores_old_failure_without_extra_queries(make_project, monkeypatch, status):
    uid = make_project(title='正在核对恢复范围')
    with Session(get_engine()) as session:
        workflow = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='failed',
                               phase='旧语义整理失败', error='旧模型错误')
        session.add(workflow)
        session.commit()
        session.refresh(workflow)
        batch = ReleaseBatch(request_id=str(uuid.uuid4()), status='running',
            items=json.dumps([dict(project_uid=uid, title='恢复中的项目', status=status,
                feedback='preparing', phase='正在核对已有结果与执行条件', workflow_id=workflow.id,
                error='旧模型错误', failure={'code': 'LLM_RATE_LIMITED'}, failed_step='split')]))
        session.add(batch)
        session.commit()
        session.refresh(batch)
        key = batch.id
    statements = []
    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement.lower())
    event.listen(get_engine(), 'before_cursor_execute', observe)
    try:
        row = release_batches.get(key)
        summary = release_batches.listing()['batches'][0]
    finally:
        event.remove(get_engine(), 'before_cursor_execute', observe)
    for value in (row, summary):
        item = value['items'][0]
        assert item['feedback'] == 'preparing' and item['error'] == ''
        assert '旧' not in item['phase'] and 'failed_step' not in item and 'failure' not in item
        current = value['current_item']
        assert current['stage'] == 'preparing' and current['percentage'] is None
        assert current['indeterminate'] is True and '核对已有结果' in current['phase']
    assert not any('workflowrun' in statement or 'steprun' in statement or 'publication' in statement
                   for statement in statements), '准备快照不能读取或借用历史工作流、步骤与发布进度'


@pytest.mark.parametrize('status', ['paused', 'stopped', 'attention'])
def test_inactive_release_snapshot_does_not_claim_workflow_is_still_running(make_project, status):
    uid = make_project(title='已单独继续的项目')
    with Session(get_engine()) as session:
        workflow = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='failed', phase='当前真实失败')
        session.add(workflow)
        session.commit()
        session.refresh(workflow)
        batch = ReleaseBatch(request_id=str(uuid.uuid4()), status=status,
            items=json.dumps([dict(project_uid=uid, title='旧批次项目', status='running', phase='旧语义整理',
                                   workflow_id=workflow.id)]))
        session.add(batch)
        session.commit()
        session.refresh(batch)
        key = batch.id
        workflow_id = workflow.id
    statements = []
    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement.lower())
    event.listen(get_engine(), 'before_cursor_execute', observe)
    try:
        detail = release_batches.get(key)
        summary = release_batches.listing()['batches'][0]
        card = release_batches.project_statuses([uid])[uid]
    finally:
        event.remove(get_engine(), 'before_cursor_execute', observe)
    for value in (detail, summary):
        assert value['items'][0]['status'] == status
        assert '旧语义整理' not in value['items'][0]['phase']
        assert value['current_item'] is None
    assert card['item_status'] == status
    assert not any('workflowrun' in sql or 'steprun' in sql for sql in statements)
    assert workflows.get(workflow_id)['status'] == 'failed', '只修正暂停调度器的展示，不改真实项目'
    with Session(get_engine()) as session:
        assert json.loads(session.get(ReleaseBatch, key).items)[0]['status'] == 'running', '普通读取不能写回历史'


@pytest.mark.parametrize('status', ['paused', 'stopped', 'attention'])
def test_inactive_mixed_group_snapshot_stays_inactive_on_all_projections(receipt, status):
    env = receipt
    group = copy.deepcopy(env['groups'][0])
    group['items'][1].update(status='running', phase='旧配音进度', error='')
    project_batches._save_group(env['id'], group['id'], status=status, items=group['items'])
    detail = project_batches.get(env['id'])['groups'][0]
    summary = project_batches.listing()['batches'][0]['groups'][0]
    for value in (detail, summary):
        assert value['items'][1]['status'] == status
        assert '旧配音进度' not in value['items'][1]['phase']
        assert value['items'][0]['status'] == 'done'
    if status != 'stopped':
        card = project_batches.project_statuses([env['uids'][1]])[env['uids'][1]]
        assert card['item_status'] == status
    assert crud.get_step(crud.get_project(env['uids'][1]).id, 'split').status == 'failed'
