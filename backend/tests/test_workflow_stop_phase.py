"""停止文案穿过实际协调器与隔离数据库，不发送模型或平台请求。"""
import copy
import json
import threading
import uuid

import pytest
from sqlmodel import Session

from dubforge import workflows, workflow_update
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.publishing import service, updates
from dubforge.runner import runner
from dubforge.workflow_models import WorkflowRun
from dubforge.workflow_plan import WorkflowError


def seed(monkeypatch, *, mode='produce', status='running', publication=None, steps=()):
    project = crud.create_project(title='停止状态测试', source_url='https://example.com/stop-phase')
    if publication:
        monkeypatch.setattr(service, 'view', lambda key: copy.deepcopy(publication))
    with Session(get_engine()) as session:
        row = WorkflowRun(project_uid=project.uid, request_id=str(uuid.uuid4()),
            recipe=json.dumps(dict(mode=mode)),
            plan=json.dumps(dict(steps=list(steps), labels={key: '当前制作步骤' for key in steps})),
            status=status, publication_id=publication['id'] if publication else '',
            phase='正在停止；已发送的投稿将等待回执' if status == 'stopping' else '原流程阶段',
            post_processing_paused=status == 'stopping')
        session.add(row)
        session.commit()
        session.refresh(row)
        key = row.id
    monkeypatch.setattr(service, 'enqueue', lambda *args, **kwargs: pytest.fail('停止不能新增平台发送'))
    return project, key


def publication(status, bvid=''):
    return dict(id='existing-publication', status=status, progress=0, error='',
                receipt=dict(bvid=bvid), chapter_sync={}, snapshot={}, execution={})


def run_cancelled(monkeypatch, project, key):
    event = threading.Event()
    event.set()
    monkeypatch.setitem(workflows._running, key, event)
    monkeypatch.setitem(workflows._leases, project.uid, key)
    workflows._execute(key, event)
    assert key not in workflows._running and project.uid not in workflows._leases
    return workflows.get(key)


def test_production_stop_request_closes_to_stopped_after_worker_exits(isolated_env, monkeypatch):
    project, key = seed(monkeypatch, steps=['download'])
    event = threading.Event()
    monkeypatch.setitem(workflows._running, key, event)
    monkeypatch.setitem(workflows._leases, project.uid, key)
    monkeypatch.setattr(workflows, '_check_configuration', lambda *args, **kwargs: None)
    requested = []

    def step(*args, **kwargs):
        requested.append(workflows.stop(key))
        return dict(status='skipped')

    monkeypatch.setattr(runner, 'start_step', step)
    workflows._execute(key, event)
    result = workflows.get(key)
    assert requested[0]['status'] == 'stopping'
    assert requested[0]['phase'] == '正在停止，等待当前制作安全收尾'
    assert result['status'] == 'paused' and result['cursor'] == 1
    assert result['phase'] == '制作已停止，已完成结果保留'
    assert result['execution']['state'] == 'paused'
    assert result['execution']['label'] == result['phase']
    assert '全流程已停止' in result['error'] and result['post_processing_paused'] is True
    assert key not in workflows._running and project.uid not in workflows._leases


@pytest.mark.parametrize('state,bvid,phase', [
    ('unknown', '', '自动推进已暂停；投稿结果待核对'),
    ('unknown', 'BV-existing', '自动推进已暂停；投稿结果待核对'),
    ('uploading', '', '自动推进已暂停；已有稿件与远端回执待核对'),
    ('draft', '', '制作已停止，发布草稿已保留'),
    ('queued', '', '自动推进已暂停；已有稿件与远端回执待核对'),
    ('published', 'BV-existing', '已停止发布后处理；已保留投稿回执'),
])
def test_cancel_terminal_retains_existing_publication_uncertainty(isolated_env, monkeypatch, state, bvid, phase):
    pub = publication(state, bvid)
    before = copy.deepcopy(pub)
    project, key = seed(monkeypatch, mode='publish', status='stopping', publication=pub)
    result = run_cancelled(monkeypatch, project, key)
    assert result['status'] == 'paused' and result['post_processing_paused'] is True
    assert result['phase'] == result['execution']['label'] == phase
    assert result['publication']['status'] == state
    assert result['publication']['receipt'] == pub['receipt']
    assert pub == before


@pytest.mark.parametrize('state,bvid,phase', [
    ('unknown', '', '自动推进已暂停；投稿结果待核对'),
    ('published', 'BV-existing', '已停止发布后处理；已保留投稿回执'),
])
def test_stop_monitoring_explains_receipt_without_resending(isolated_env, monkeypatch, state, bvid, phase):
    _, key = seed(monkeypatch, mode='publish', status='monitoring', publication=publication(state, bvid))
    result = workflows.stop(key)
    assert result['status'] == 'attention' and result['post_processing_paused'] is True
    assert result['phase'] == phase
    assert result['publication']['status'] == state


def test_update_unknown_stays_explicit_when_cancelled_before_execution(isolated_env, monkeypatch):
    task = dict(id='original-update', status='unknown', bvid='BV-existing', phase='原稿结果不明')
    monkeypatch.setattr(workflows, '_update_task', lambda run: task)
    project, key = seed(monkeypatch, mode='update', status='stopping')
    result = run_cancelled(monkeypatch, project, key)
    assert result['status'] == 'paused'
    assert result['phase'] == result['execution']['label'] == '自动推进已暂停；原稿更新结果待核对'
    assert result['publication_update']['status'] == 'unknown'


def test_update_stop_after_delivery_does_not_claim_unknown_remote_has_stopped(isolated_env, monkeypatch):
    task = dict(id='original-update', status='prepared', bvid='BV-existing', phase='等待更新')
    monkeypatch.setattr(workflows, '_update_task', lambda run: task)
    project, key = seed(monkeypatch, mode='update')
    event = threading.Event()
    monkeypatch.setitem(workflows._running, key, event)
    monkeypatch.setitem(workflows._leases, project.uid, key)
    paused = []

    def deliver(*args):
        task['status'] = 'unknown'
        event.set()
        return task

    monkeypatch.setattr(workflow_update, 'deliver', deliver)
    monkeypatch.setattr(updates, 'action', lambda ident, body: paused.append((ident, body['action'])))
    workflows._execute(key, event)
    result = workflows.get(key)
    assert result['status'] == 'attention' and result['post_processing_paused'] is True
    assert result['phase'] == '自动推进已暂停；原稿更新结果待核对'
    assert result['publication_update']['status'] == 'unknown'
    assert paused == [('original-update', 'pause')]


def test_uncancelled_failure_keeps_actual_step_phase_and_error(isolated_env, monkeypatch):
    project, key = seed(monkeypatch, steps=['download'])
    event = threading.Event()
    monkeypatch.setitem(workflows._running, key, event)
    monkeypatch.setitem(workflows._leases, project.uid, key)
    monkeypatch.setattr(workflows, '_check_configuration', lambda *args, **kwargs: None)

    def fail(*args, **kwargs):
        raise WorkflowError('实际制作失败', code='WORKFLOW_STEP_FAILED')

    monkeypatch.setattr(runner, 'start_step', fail)
    workflows._execute(key, event)
    result = workflows.get(key)
    assert result['status'] == 'failed' and result['phase'] == '当前制作步骤'
    assert result['error'] == '实际制作失败' and result['execution']['state'] == 'failed'


@pytest.mark.parametrize('state,bvid,phase', [
    (None, '', '制作已停止，已完成结果保留'),
    ('draft', '', '制作已停止，发布草稿已保留'),
    ('queued', '', '自动推进已暂停；已有稿件与远端回执待核对'),
    ('unknown', '', '自动推进已暂停；投稿结果待核对'),
    ('unknown', 'BV-existing', '自动推进已暂停；投稿结果待核对'),
    ('published', 'BV-existing', '已停止发布后处理；已保留投稿回执'),
])
def test_legacy_paused_phase_is_readonly_in_get_and_dashboard(isolated_env, monkeypatch, state, bvid, phase):
    from sqlalchemy import event as sql_event
    from dubforge.publishing.models import Publication

    pub = publication(state, bvid) if state else None
    project, key = seed(monkeypatch, mode='publish' if pub else 'produce', status='paused', publication=pub)
    with Session(get_engine()) as session:
        row = session.get(WorkflowRun, key)
        row.phase = workflows._LEGACY_STOPPING_PHASE
        row.error = '原始停止原因保持'
        row.execution = json.dumps(dict(schema=1, owner='workflow', run_id=key, state='paused',
            phase='production.assemble', label='历史合成阶段', detail='原始停止原因保持',
            history=[dict(phase='production.tts', label='历史配音阶段', state='completed')]))
        session.add(row)
        if pub:
            session.add(Publication(id=pub['id'], project_uid=project.uid, status=state,
                                    receipt=json.dumps(pub['receipt'])))
        session.commit()
        session.refresh(row)
        before = row.model_dump()

    def no_write(connection, cursor, statement, *args):
        assert statement.lstrip().split()[0].upper() not in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}

    engine = get_engine()
    sql_event.listen(engine, 'before_cursor_execute', no_write)
    try:
        result = workflows.get(key)
        dashboard = workflows.status(project.uid)['run']
    finally:
        sql_event.remove(engine, 'before_cursor_execute', no_write)
    assert result['phase'] == dashboard['phase'] == phase
    assert result['error'] == dashboard['error'] == before['error']
    assert result['execution']['label'] == dashboard['execution']['label'] == '历史合成阶段'
    assert result['execution']['history'][0]['label'] == '历史配音阶段'
    with Session(engine) as session:
        assert session.get(WorkflowRun, key).model_dump() == before
        if pub:
            assert session.get(Publication, pub['id']).status == state


def test_legacy_update_unknown_phase_keeps_remote_uncertainty_and_history(isolated_env, monkeypatch):
    task = dict(id='original-update', status='unknown', bvid='BV-existing', phase='原稿结果不明')
    monkeypatch.setattr(workflows, '_update_task', lambda run: task)
    project, key = seed(monkeypatch, mode='update', status='paused')
    with Session(get_engine()) as session:
        row = session.get(WorkflowRun, key)
        row.phase, row.error = workflows._LEGACY_STOPPING_PHASE, '保留unknown原因'
        session.add(row)
        session.commit()
        session.refresh(row)
        before = row.model_dump()
    assert workflows.get(key)['phase'] == '自动推进已暂停；原稿更新结果待核对'
    dashboard = workflows.status(project.uid)['run']
    assert dashboard['phase'] == '自动推进已暂停；原稿更新结果待核对'
    assert dashboard['publication_update']['status'] == 'unknown'
    with Session(get_engine()) as session:
        assert session.get(WorkflowRun, key).model_dump() == before


@pytest.mark.parametrize('status,phase', [
    ('running', '正在停止；已发送的投稿将等待回执'),
    ('stopping', '正在停止；已发送的投稿将等待回执'),
    ('paused', '正在停止；已发送的投稿将等待回执；另有说明'),
    ('paused', '本次仍有手动确认事项'),
])
def test_legacy_projection_does_not_rewrite_other_statuses_or_phase_text(isolated_env, monkeypatch, status, phase):
    _, key = seed(monkeypatch, status=status)
    with Session(get_engine()) as session:
        row = session.get(WorkflowRun, key)
        row.phase = phase
        session.add(row)
        session.commit()
    assert workflows.get(key)['phase'] == phase


def test_unknown_with_bv_returned_during_stop_is_not_presented_as_confirmed_receipt(isolated_env, monkeypatch):
    pub = {**publication('uploaded'), 'revision': 1}
    project, key = seed(monkeypatch, mode='publish', publication=pub)
    event = threading.Event()
    monkeypatch.setitem(workflows._running, key, event)
    monkeypatch.setitem(workflows._leases, project.uid, key)
    sent = []
    monkeypatch.setattr(service, 'plan', lambda *args, **kwargs: dict(fingerprint='confirmed-inputs'))
    monkeypatch.setattr(service, 'enqueue', lambda ident, body: sent.append((ident, body['action'])))

    def reply(ident, cancel):
        assert ident == pub['id'] and cancel is event
        pub.update(status='unknown', receipt=dict(bvid='BV-unverified'))
        event.set()
        return copy.deepcopy(pub)

    monkeypatch.setattr(workflows, '_wait_publication', reply)
    workflows._execute(key, event)
    result = workflows.get(key)
    assert result['status'] == 'attention' and result['post_processing_paused'] is True
    assert result['phase'] == result['execution']['label'] == '自动推进已暂停；投稿结果待核对'
    assert result['publication']['status'] == 'unknown'
    assert result['publication']['receipt'] == dict(bvid='BV-unverified')
    assert sent == [(pub['id'], 'submit')]
    assert key not in workflows._running and project.uid not in workflows._leases
