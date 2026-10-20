"""混合批次用本地流程替身验证调度隔离和持久恢复，禁止真实投稿。"""
import copy
import threading
import time
import uuid

import pytest

from dubforge import configuration, project_batches as batches, workflows
from dubforge.errors import DubForgeError
from test_configuration import save
from test_release_batches import batch_env
from test_release_batch_targets import targets


def prepared(uids):
    body = dict(project_uids=uids, order_confirmed=True)
    plan = batches.preview(body)
    assert not plan['blockers'], plan['blockers']
    creation = dict(body,request_id=str(uuid.uuid4()),confirmed=True,fingerprint=plan['fingerprint'])
    row = batches.create(creation)
    ready = batches.recovery_plan(row['id'])
    assert ready['fingerprint'] == plan['fingerprint']
    return row, creation, ready


def begin(row, plan=None, **extra):
    plan = plan or batches.recovery_plan(row['id'])
    body = dict(request_id=str(uuid.uuid4()),confirmed=True,rights_confirmed=True,fingerprint=plan['fingerprint'],**extra)
    return batches.start(row['id'],body),body


def finish(key):
    limit = time.monotonic() + 10
    while any(parent == key for parent, group in batches._running) and time.monotonic() < limit:
        time.sleep(.01)
    assert not any(parent == key for parent, group in batches._running)
    return batches.get(key)


@pytest.fixture
def mixed(targets, make_project, monkeypatch):
    batches.recover()
    independent = [make_project(title=f'独立视频{i}') for i in range(2)]
    for uid in independent:
        version = configuration.resolve(uid)['values']['assembly.versions'][0]['id']
        save('project:' + uid, {'run.mode':'produce','assembly.target_version_ids':[version]})
    original = workflows._launch
    def launch(key, uid):
        if uid in independent:
            targets['started'].append(uid)
            workflows._update(key,status='completed',phase='制作完成')
        else:
            original(key,uid)
    monkeypatch.setattr(workflows,'_launch',launch)
    yield dict(**targets,independent=independent,all=[*targets['uids'],*independent])
    for parent, group in list(batches._running):
        batches.stop(parent)
        finish(parent)
    assert not batches._leases
    assert not batches._children


def test_mixed_five_projects_group_and_complete_without_authority_promotion(mixed):
    row, creation, plan = prepared(mixed['all'])
    assert len(row['groups']) == 3
    assert [g['project_uids'] for g in row['groups']] == [mixed['uids'],*[ [uid] for uid in mixed['independent']]]
    assert not mixed['started']
    assert batches.create(creation)['id'] == row['id']
    _, start = begin(row,plan)
    result = finish(row['id'])
    assert result['status'] == 'done', result['error']
    assert mixed['orders'] and len(mixed['orders']) == 1
    assert [uid for uid in mixed['started'] if uid in mixed['uids']] == mixed['uids']
    for group in result['groups'][1:]:
        assert group['workflow']['recipe']['mode'] == 'produce'
        assert group['items'][0]['status'] == 'done'
    assert batches.start(row['id'],start)['status'] == 'done'
    assert len(mixed['started']) == 5


def test_collection_failure_does_not_block_independent_groups(mixed,monkeypatch):
    from dubforge import release_batches
    def fail(key,event):
        try:
            release_batches._update(key,status='attention',phase='等待核对回执',error='隔离错误')
        finally:
            with release_batches._lock:
                release_batches._running.pop(key,None)
                for uid, lease in list(release_batches._leases.items()):
                    if lease == key:
                        release_batches._leases.pop(uid,None)
                        release_batches._children.pop(uid,None)
    monkeypatch.setattr(release_batches,'_execute',fail)
    row,_,plan = prepared(mixed['all'])
    begin(row,plan)
    result = finish(row['id'])
    assert result['status'] == 'attention'
    assert result['groups'][0]['status'] == 'attention'
    assert all(g['status'] == 'done' for g in result['groups'][1:])
    assert set(mixed['started']) == set(mixed['independent'])


def test_changed_plan_and_same_request_different_confirmation_are_rejected(mixed):
    row,creation,_ = prepared(mixed['all'])
    with pytest.raises(DubForgeError):
        batches.create(dict(creation,project_uids=list(reversed(mixed['all']))))
    uid = mixed['independent'][0]
    save('project:' + uid, {'assemble.crf':27})
    current = batches.recovery_plan(row['id'])
    with pytest.raises(DubForgeError):
        batches.start(row['id'],dict(request_id=str(uuid.uuid4()),confirmed=True,rights_confirmed=True,fingerprint='old'))
    assert not mixed['started']


def test_parent_restart_pauses_and_never_starts_children(mixed):
    row,_,_ = prepared(mixed['all'])
    batches._save_group(row['id'],row['groups'][0]['id'],status='running')
    batches.recover()
    assert batches.get(row['id'])['status'] == 'paused'
    assert not mixed['started'] and not mixed['orders']


def test_candidates_and_listing_do_not_scan_artifacts(mixed,monkeypatch):
    row,_,_ = prepared(mixed['all'])
    def forbidden(*args,**kwargs):
        raise AssertionError('列表/候选不应计算完整产物方案')
    monkeypatch.setattr(workflows,'prepared_plan',forbidden)
    assert len(batches.candidates(mixed['all'])['items']) == 5
    assert batches.listing(row['request_id'])['batches'][0]['id'] == row['id']


def test_failed_creation_is_durable_terminal_and_same_uuid_cannot_later_succeed(mixed,monkeypatch):
    body = dict(project_uids=mixed['independent'],order_confirmed=True)
    plan = batches.preview(body)
    creation = dict(body,request_id=str(uuid.uuid4()),confirmed=True,fingerprint=plan['fingerprint'])
    attempts = []
    def fail(body):
        attempts.append(1)
        raise batches.ProjectBatchError('预检已变化',code='PROJECT_BATCH_PLAN_CHANGED')
    monkeypatch.setattr(batches,'preview',fail)
    with pytest.raises(DubForgeError):
        batches.create(creation)
    receipt = batches.listing(creation['request_id'])['batches'][0]
    assert receipt['status'] == 'creation_failed'
    assert receipt['creation_error']['code'] == 'PROJECT_BATCH_PLAN_CHANGED'
    assert batches.create(creation)['status'] == 'creation_failed'
    assert len(attempts) == 1
    assert not batches.listing()['batches']
    with pytest.raises(DubForgeError):
        batches.start(receipt['id'],dict(request_id=str(uuid.uuid4()),confirmed=True,
            rights_confirmed=True,fingerprint=plan['fingerprint']))
    assert not mixed['started']


def test_inflight_creation_and_late_worker_cannot_revive_terminal_request(mixed,monkeypatch):
    body = dict(project_uids=mixed['independent'],order_confirmed=True)
    plan = batches.preview(body)
    creation = dict(body,request_id=str(uuid.uuid4()),confirmed=True,fingerprint=plan['fingerprint'])
    entered,release = threading.Event(),threading.Event()
    def delayed(body):
        entered.set()
        assert release.wait(8)
        return copy.deepcopy(plan)
    monkeypatch.setattr(batches,'preview',delayed)
    results = []
    worker = threading.Thread(target=lambda:results.append(batches.create(creation)))
    worker.start()
    try:
        assert entered.wait(5)
        assert batches.create(creation)['status'] == 'creating'
        assert batches.listing(creation['request_id'])['batches'][0]['status'] == 'creating'
        batches.recover()
        assert batches.listing(creation['request_id'])['batches'][0]['status'] == 'creation_failed'
    finally:
        release.set()
        worker.join(8)
    assert not worker.is_alive()
    assert results[0]['status'] == 'creation_failed'
    assert not mixed['started']


def test_api_routes_preserve_read_only_preview_and_create_receipts(mixed):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    ids = mixed['independent']
    response = client.get('/api/project-batches/candidates',params={'uids':','.join(ids)})
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    body = dict(project_uids=ids,order_confirmed=True)
    plan = client.post('/api/project-batches/plan',json=body).json()
    response = client.post('/api/project-batches',json=dict(body,request_id=str(uuid.uuid4()),
        fingerprint=plan['fingerprint'],confirmed=True))
    assert response.status_code == 200 and response.json()['status'] == 'ready'
    row = response.json()
    assert client.get('/api/project-batches/'+row['id']).status_code == 200
    assert client.post('/api/project-batches/'+row['id']+'/plan',json={}).json()['fingerprint'] == plan['fingerprint']
    assert not mixed['started']


def test_restart_pauses_child_post_processing_including_lost_child_receipt(mixed):
    import json
    from sqlmodel import Session
    from dubforge.db import get_engine
    from dubforge.workflow_models import WorkflowRun
    row,_,_ = prepared(mixed['independent'])
    group = row['groups'][0]
    with Session(get_engine()) as session:
        child = WorkflowRun(project_uid=group['project_uids'][0],status='monitoring',
            request_id=batches._child_request(row['id'],group['id']),
            recipe=json.dumps(group['items'][0]['recipe']),plan=json.dumps(group['plan']))
        session.add(child)
        session.commit()
        session.refresh(child)
        child_id = child.id
    batches._save_group(row['id'],group['id'],status='running')
    batches.recover()
    child = workflows.get(child_id)
    assert child['status'] == 'attention'
    assert child['post_processing_paused'] is True
    assert batches.get(row['id'])['groups'][0]['workflow_id'] == child_id
    assert not mixed['started']
