"""排队项不能冒领旧流程的失败；本次实际失败和所选草稿身份仍须保留。"""
import json
import uuid

import pytest
from sqlmodel import Session

from dubforge import release_batches as batches, workflows
from dubforge.db import get_engine
from dubforge.release_batch_models import ReleaseBatch
from dubforge.workflow_models import WorkflowRun
from test_release_batches import batch_env, prepared


HISTORICAL_ERROR = '制作设置在运行期间发生变化，已暂停；请重新预检后继续'


def _old_run(uid, *, publication_id=''):
    with Session(get_engine()) as session:
        row = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='failed',
                          phase='翻译', error=HISTORICAL_ERROR, publication_id=publication_id)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def test_new_batch_and_creation_do_not_copy_historical_workflow_errors(batch_env):
    run_id = _old_run(batch_env['uids'][1])
    row, _, _ = prepared(batch_env)
    pending = row['items'][1]
    assert pending['workflow_id'] == run_id
    assert pending['status'] == 'queued'
    assert pending['error'] == ''
    assert pending['phase'] == '等待前项完成'
    with Session(get_engine()) as session:
        saved = json.loads(session.get(ReleaseBatch, row['id']).items)[1]
        assert saved['error'] == '' and saved['phase'] == '等待前项完成'
        assert session.get(WorkflowRun, run_id).error == HISTORICAL_ERROR
    assert not batch_env['started']


def _legacy_batch(make_project, *, status='queued'):
    first, second = make_project(title='本次执行项'), make_project(title='等待项目')
    run_id = _old_run(second, publication_id='historical-publication')
    items = [dict(project_uid=first, title='本次执行项', status='running', phase='语义整理', error=''),
             dict(project_uid=second, title='等待项目', status=status, phase='翻译', error=HISTORICAL_ERROR,
                  workflow_id=run_id, publication_id='explicit-new-review-draft')]
    with Session(get_engine()) as session:
        row = ReleaseBatch(request_id=str(uuid.uuid4()), status='running' if status == 'queued' else 'attention',
                           cursor=0 if status == 'queued' else 1, items=json.dumps(items, ensure_ascii=False))
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id, run_id, row.items


def test_existing_queued_errors_are_hidden_read_only_in_detail_and_listing(make_project, monkeypatch):
    key, run_id, original_items = _legacy_batch(make_project)
    monkeypatch.setattr(workflows, 'get', lambda *_: (_ for _ in ()).throw(AssertionError('展示不能读取旧流程重型详情')))
    for result in [batches.get(key), batches.listing()['batches'][0]]:
        pending = result['items'][1]
        assert pending['status'] == 'queued'
        assert pending['error'] == '' and pending['phase'] == '等待前项完成'
        assert pending['publication_id'] == 'explicit-new-review-draft'
    with Session(get_engine()) as session:
        assert session.get(ReleaseBatch, key).items == original_items
        assert session.get(WorkflowRun, run_id).error == HISTORICAL_ERROR


@pytest.mark.parametrize('status', ['attention', 'done'])
def test_actual_item_result_is_not_cleared_by_display(make_project, status):
    key, _, _ = _legacy_batch(make_project, status=status)
    for result in [batches.get(key), batches.listing()['batches'][0]]:
        item = result['items'][1]
        assert item['status'] == status
        assert item['error'] == HISTORICAL_ERROR and item['phase'] == '翻译'
        assert item['publication_id'] == 'explicit-new-review-draft'


def test_recovery_preview_clears_resolved_attempt_error_without_changing_history(batch_env):
    row, _, _ = prepared(batch_env)
    items = row['items']
    items[0].update(status='attention', error=HISTORICAL_ERROR, phase='翻译')
    batches._update(row['id'], status='attention', items=items)
    plan = batches.recovery_plan(row['id'], online=False)
    assert not plan['blockers']
    assert all(item['status'] == 'queued' and item['error'] == '' and item['phase'] == '等待前项完成'
               for item in plan['items'])
    assert batches.get(row['id'])['items'][0]['error'] == HISTORICAL_ERROR
    assert not batch_env['started']


def test_shared_old_workflow_in_another_active_batch_cannot_overwrite_waiting_item(make_project):
    key, run_id, original_items = _legacy_batch(make_project)
    waiting = json.loads(original_items)[1]
    with Session(get_engine()) as session:
        run = session.get(WorkflowRun, run_id)
        run.status, run.phase = 'running', '本次配音'
        session.add(run)
        another = ReleaseBatch(request_id=str(uuid.uuid4()), status='running',
            items=json.dumps([{**waiting, 'status': 'running', 'error': ''}]))
        session.add(another)
        session.commit()
    summaries = {row['id']: row for row in batches.listing()['batches']}
    item = summaries[key]['items'][1]
    assert item['phase'] == '等待前项完成' and item['error'] == ''
    assert item['publication_id'] == 'explicit-new-review-draft'
