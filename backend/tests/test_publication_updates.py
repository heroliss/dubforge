"""更新原BV的完整编排；隔离DB、伪上传/平台，不访问真实账户。"""
import copy
import json
import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from dubforge.db import get_engine
from dubforge.errors import DubForgeError
from dubforge.publishing import service as pub, studio, updates, update_delivery, transport
from dubforge.publishing.models import Publication, PublicationUpdate, PublishChange
from test_publishing import setup_publish


@pytest.fixture
def env(setup_publish, monkeypatch):
    uid, source, artifacts, calls = setup_publish
    preview = pub.plan(source['id'])
    transfers = {str(p['artifact_id']): dict(cid=1000 + index, filename=f'old-{index}', title=p['title'])
                 for index, p in enumerate(preview['snapshot']['parts'])}
    pub._update(source['id'], status='rejected', snapshot=preview['snapshot'],
        transfers=transfers, receipt=dict(aid=222, bvid='BV1234567890'))
    remote = dict(aid=222, bvid='BV1234567890', mid='123', version='v1', state=0,
        settings=preview['snapshot']['settings'], edit_supported=True, collection=None, in_season=False,
        videos=[dict(**v, duration=100) for v in transfers.values()])
    monkeypatch.setattr(studio, 'read', lambda account, action, body, **kw: copy.deepcopy(remote))
    original = transport.call
    writes, uploads, deliveries = [], [], []
    def remote_call(account, operation, payload):
        if operation != 'write':
            raise AssertionError(operation)
        writes.append(copy.deepcopy(payload))
        if payload['kind'] == 'replace':
            remote['videos'] = copy.deepcopy(payload['videos'])
        remote['settings'].update(payload['values'])
        remote['version'] = 'v2'
        return dict(bvid=remote['bvid'], aid=remote['aid'], accepted=True, verified=True)
    monkeypatch.setattr(studio, '_remote', remote_call)
    def call(operation, payload=None, **kw):
        if operation == 'upload':
            uploads.append(payload)
            return dict(cid=2000 + payload['artifact_id'], filename=f'new-{payload["artifact_id"]}', title=payload['title'])
        if operation == 'status':
            return copy.deepcopy(remote)
        return original(operation, payload, **kw)
    monkeypatch.setattr(transport, 'call', call)
    def delivery(snapshot, execution, *, save, cancel=None, read_only=False):
        deliveries.append(copy.deepcopy(snapshot))
        for node in ('review', 'chapters', 'collection', 'verify'):
            execution['stages'][node]['status'] = 'completed'
        execution.update(outcome='completed', phase='原稿更新已核验', error='', error_code='')
        save(execution)
        return execution
    monkeypatch.setattr(update_delivery, 'run', delivery)
    return dict(uid=uid, source=source, artifacts=artifacts, remote=remote,
                writes=writes, uploads=uploads, deliveries=deliveries)


def prepare(env, **extra):
    return updates.plan(env['source']['id'], dict(replacements=[dict(
        old_cid=env['remote']['videos'][0]['cid'], artifact_id=env['artifacts'][0].id)], **extra))


def finish(key):
    deadline = time.monotonic() + 8
    while key in updates._running and time.monotonic() < deadline:
        time.sleep(.005)
    assert key not in updates._running
    return updates.view(key)


def start(task, action='start'):
    updates.action(task['id'], dict(action=action, confirmed=True, fingerprint=task['fingerprint']))
    return finish(task['id'])


def test_plan_is_local_confirm_once_updates_original_and_preserves_other_parts_and_history(env):
    source = pub.view(env['source']['id'])
    task = prepare(env, values=dict(description='新版简介'))
    assert task['status'] == 'prepared' and not env['uploads'] and not env['writes']
    assert task['preview']['parts'][0]['replaced']
    assert not task['preview']['parts'][1]['replaced']
    assert 'new-' not in json.dumps(task)
    with pytest.raises(DubForgeError) as error:
        updates.action(task['id'], dict(action='start', fingerprint=task['fingerprint']))
    assert error.value.code == 'PUBLISH_UPDATE_CONFIRM'
    result = start(task)
    assert result['status'] == 'completed'
    assert len(env['uploads']) == 1 and len(env['writes']) == 1
    payload = env['writes'][0]
    assert payload['bvid'] == 'BV1234567890' and payload['kind'] == 'replace'
    assert [p['cid'] for p in payload['videos']][1:] == [p['cid'] for p in env['remote']['videos']][1:]
    assert payload['values'] == {'description': '新版简介'}
    current = pub.view(env['source']['id'])
    assert current['snapshot'] == source['snapshot']
    assert current['latest_update']['id'] == task['id']
    assert current['status'] != 'rejected'
    assert 'filename' not in json.dumps(result['execution'])
    assert start(result, 'retry')['status'] == 'completed'
    assert len(env['writes']) == 1


def test_upload_failure_preserves_complete_uploads_and_retry_does_not_resend_completed_parts(env, monkeypatch):
    task = updates.plan(env['source']['id'], dict(replacements=[dict(old_cid=1000 + i, artifact_id=a.id)
        for i, a in enumerate(env['artifacts'][:2])]))
    original = transport.call
    attempts = []
    fail = True
    def call(operation, payload=None, **kw):
        nonlocal fail
        if operation == 'upload':
            attempts.append(payload['artifact_id'])
            if len(attempts) == 2 and fail:
                fail = False
                raise pub.PublishError('隔离网络中断')
        return original(operation, payload, **kw)
    monkeypatch.setattr(transport, 'call', call)
    result = start(task)
    assert result['status'] == 'failed' and not env['writes']
    result = start(task, 'retry')
    assert result['status'] == 'completed'
    assert attempts == [env['artifacts'][0].id, env['artifacts'][1].id, env['artifacts'][1].id]


def test_unknown_edit_requires_reconcile_and_never_reposts_or_writes_followups_in_that_action(env, monkeypatch):
    task = prepare(env)
    original = studio._remote
    def lost(account, operation, payload):
        original(account, operation, payload)
        raise pub.PublishError('回执丢失', code='PUBLISH_WRITE_UNKNOWN')
    monkeypatch.setattr(studio, '_remote', lost)
    result = start(task)
    assert result['status'] == 'unknown' and not env['deliveries']
    assert result['execution']['stages']['edit']['status'] == 'unknown'
    with pytest.raises(DubForgeError) as error:
        updates.action(task['id'], dict(action='retry', confirmed=True, fingerprint=task['fingerprint']))
    assert error.value.code == 'PUBLISH_WRITE_UNKNOWN'
    result = start(task, 'reconcile')
    assert result['status'] == 'paused' and not env['deliveries']
    assert len(env['writes']) == 1
    result = start(task, 'retry')
    assert result['status'] == 'completed' and len(env['writes']) == 1


def test_chapter_failure_retries_delivery_only_and_keeps_new_frozen_timeline(env, monkeypatch):
    task = prepare(env)
    original = update_delivery.run
    attempts = 0
    def delivery(snapshot, execution, **kw):
        nonlocal attempts
        attempts += 1
        assert snapshot['parts'][0]['replaced'] is True
        assert 'output_cuts' in snapshot['parts'][0] and 'chapter_items' in snapshot['parts'][0]
        if attempts == 1:
            execution.update(outcome='failed', error='章节写入失败', phase='视频已更新，章节待处理')
            return execution
        return original(snapshot, execution, **kw)
    monkeypatch.setattr(update_delivery, 'run', delivery)
    result = start(task)
    assert result['status'] == 'failed' and result['execution']['stages']['edit']['status'] == 'completed'
    result = start(task, 'retry')
    assert result['status'] == 'completed' and attempts == 2
    assert len(env['uploads']) == 1 and len(env['writes']) == 1


def test_retry_receipt_immediately_replaces_failed_progress_before_worker_starts(env, monkeypatch):
    task = prepare(env)
    with Session(get_engine()) as session:
        row = session.get(PublicationUpdate, task['id'])
        execution = json.loads(row.execution)
        execution['stages']['upload'].update(status='completed', progress=100)
        execution['stages']['edit'].update(status='completed')
        execution['stages']['review'].update(status='completed')
        execution['stages']['chapters'].update(status='failed', label='章节待处理',
            error='模型余额不足', error_code='LLM_QUOTA_EXHAUSTED', progress=12)
        execution.update(error='模型余额不足', error_code='LLM_QUOTA_EXHAUSTED', outcome='failed')
        execution['delivery'] = dict(parts=[dict(cid=2001, status='failed', error='模型余额不足',
            error_code='LLM_QUOTA_EXHAUSTED', change_id='')], collection=dict(status='skipped'),
            execution=dict(schema=1, state='failed', phase='chapters.adapt.failed', label='章节整理未完成',
                detail='模型余额不足', owner='publication_update', run_id=task['id']))
        row.execution, row.status, row.error = json.dumps(execution), 'failed', '模型余额不足'
        session.add(row); session.commit()
    queued = []
    monkeypatch.setattr(updates._pool, 'submit', lambda *args: queued.append(args))
    body = dict(action='retry', confirmed=True, fingerprint=task['fingerprint'])
    try:
        received = updates.action(task['id'], body)
        repeated = updates.action(task['id'], body)
        current = received['execution']
        assert received['status'] == repeated['status'] == 'queued'
        assert received['error'] == ''
        assert current['error'] == '' and current['error_code'] == '' and current['outcome'] == ''
        assert current['stages']['chapters']['status'] == 'queued'
        assert current['stages']['chapters']['error'] == ''
        assert current['stages']['upload']['status'] == current['stages']['edit']['status'] == 'completed'
        assert current['stages']['upload']['progress'] == 100
        assert current['delivery']['parts'][0]['status'] == 'pending'
        assert current['delivery']['parts'][0]['error_code'] == ''
        assert current['delivery']['execution']['state'] == 'queued'
        assert current['delivery']['execution']['phase'] == 'update.preparing'
        assert len(queued) == 1
        assert not env['uploads'] and not env['writes']
    finally:
        updates._running.pop(task['id'], None)


def test_queue_rejection_does_not_leave_preparing_feedback_or_execution_lease(env, monkeypatch):
    task = prepare(env)
    def unavailable(*args):
        raise RuntimeError('执行器已关闭')
    monkeypatch.setattr(updates._pool, 'submit', unavailable)
    with pytest.raises(DubForgeError) as rejected:
        updates.action(task['id'], dict(action='start', confirmed=True, fingerprint=task['fingerprint']))
    assert rejected.value.code == 'PUBLISH_UPDATE_QUEUE'
    current = updates.view(task['id'])
    assert current['status'] == 'paused' and current['error_code'] == 'PUBLISH_UPDATE_QUEUE'
    assert current['execution']['stages']['upload']['status'] == 'paused'
    assert task['id'] not in updates._running
    assert not env['uploads'] and not env['writes']


@pytest.mark.parametrize('change', ['file', 'remote', 'revision'])
def test_changes_after_confirmation_stop_before_upload_or_write(env, change):
    task = prepare(env)
    if change == 'file':
        from dubforge.domain import crud
        (crud.project_dir(env['uid']) / env['artifacts'][0].rel_path).write_bytes(b'changed')
    elif change == 'remote':
        env['remote']['version'] = 'v99'
    else:
        with Session(get_engine()) as session:
            row = session.get(Publication, env['source']['id'])
            row.revision += 1
            session.add(row); session.commit()
    try:
        result = start(task)
        assert result['status'] == 'failed'
    except DubForgeError:
        assert change == 'revision'
    assert not env['uploads'] and not env['writes']


def test_same_bv_is_exclusive_but_other_projects_can_be_edited_while_waiting(env, monkeypatch):
    task = prepare(env)
    with Session(get_engine()) as session:
        row = session.get(PublicationUpdate, task['id'])
        row.status = 'waiting'
        session.add(row); session.commit()
    with pytest.raises(DubForgeError) as error:
        prepare(env)
    assert error.value.code == 'PUBLISH_UPDATE_BUSY'
    with pytest.raises(DubForgeError):
        studio.prepare('123', dict(bvid='BV1234567890', kind='edit', values={'description': '争写'}))
    from dubforge import runtime_edit
    runtime_edit.assert_editable('project:' + env['uid'])


def test_recovery_marks_uncertain_write_unknown_and_other_interrupted_tasks_paused(env):
    task = prepare(env)
    with Session(get_engine()) as session:
        change = PublishChange(account_id='123', bvid='BV1234567890', kind='replace', status='committing')
        session.add(change); session.commit(); session.refresh(change)
        row = session.get(PublicationUpdate, task['id'])
        execution = json.loads(row.execution)
        execution['change_id'] = change.id
        row.execution, row.status = json.dumps(execution), 'submitting'
        session.add(row); session.commit()
    updates.recover()
    assert updates.view(task['id'])['status'] == 'unknown'


def test_pause_and_recovery_do_not_hide_uncertain_chapter_write(env):
    task = prepare(env)
    with Session(get_engine()) as session:
        change = PublishChange(account_id='123', bvid='BV1234567890', kind='chapters', status='unknown')
        session.add(change); session.commit(); session.refresh(change)
        row = session.get(PublicationUpdate, task['id'])
        execution = json.loads(row.execution)
        execution['stages']['edit']['status'] = 'completed'
        execution['delivery'] = dict(parts=[dict(status='unknown', change_id=change.id)])
        row.execution, row.status = json.dumps(execution), 'delivering'
        session.add(row); session.commit()
    assert updates.action(task['id'], {'action': 'pause'})['status'] == 'unknown'
    updates.recover()
    assert updates.view(task['id'])['status'] == 'unknown'
    with pytest.raises(DubForgeError):
        updates.action(task['id'], dict(action='retry', confirmed=True, fingerprint=task['fingerprint']))


def test_pending_other_remote_change_blocks_before_upload(env):
    with Session(get_engine()) as session:
        session.add(PublishChange(account_id='123', bvid='BV1234567890', kind='edit', status='unknown'))
        session.commit()
    with pytest.raises(DubForgeError) as error:
        prepare(env)
    assert error.value.code == 'PUBLISH_WRITE_UNKNOWN'
    assert not env['uploads']


def test_update_then_chapters_only_reuses_new_binding_without_reupload(env):
    task = prepare(env)
    assert start(task)['status'] == 'completed'
    second = updates.plan(env['source']['id'], dict(chapters={'enabled': True}))
    with Session(get_engine()) as session:
        snapshot = json.loads(session.get(PublicationUpdate, second['id']).plan)
    first = snapshot['parts'][0]
    assert first['old_cid'] == env['remote']['videos'][0]['cid'] != 1000
    assert first['replaced'] is False and first['sha256'] == env['artifacts'][0].sha256
    assert start(second)['status'] == 'completed'
    assert len(env['uploads']) == 1 and len(env['writes']) == 1


def test_postprocessing_freezes_target_bv_but_does_not_lock_production_files(env):
    task = prepare(env)
    with Session(get_engine()) as session:
        row = session.get(PublicationUpdate, task['id'])
        row.status = 'delivering'
        execution = json.loads(row.execution)
        execution['stages']['edit']['status'] = 'completed'
        row.execution = json.dumps(execution)
        session.add(row); session.commit()
    from dubforge import runtime_edit
    runtime_edit.assert_editable('project:' + env['uid'])
    with pub.output_replacement(env['uid']):
        pass
    with pytest.raises(DubForgeError):
        studio.prepare('123', dict(bvid='BV1234567890', kind='edit', values={'description': '并发修改'}))


def test_metadata_only_plan_uses_existing_bv_without_upload(env):
    task = updates.plan(env['source']['id'], dict(values={'description': '只改文案'}))
    result = start(task)
    assert result['status'] == 'completed' and not env['uploads']
    assert env['writes'][0]['kind'] == 'edit'


def test_accepted_metadata_without_matching_readback_stays_unknown_and_reconcile_is_read_only(env, monkeypatch):
    task = updates.plan(env['source']['id'], dict(values={'description': '待核对文案'}))
    def accepted(account, operation, payload):
        env['writes'].append(copy.deepcopy(payload))
        return dict(bvid='BV1234567890', aid=222, accepted=True)
    monkeypatch.setattr(studio, '_remote', accepted)
    result = start(task)
    assert result['status'] == 'unknown' and not env['deliveries']
    assert result['execution']['stages']['edit']['status'] != 'completed'
    assert updates.action(task['id'], {'action': 'pause'})['status'] == 'unknown'
    with pytest.raises(DubForgeError):
        start(task, 'retry')
    assert start(task, 'reconcile')['status'] == 'unknown'
    env['remote']['settings']['description'] = '待核对文案'
    assert start(task, 'reconcile')['status'] == 'paused'
    assert not env['deliveries'] and len(env['writes']) == 1
    assert start(task, 'retry')['status'] == 'completed'
    assert len(env['writes']) == 1 and not env['uploads']


def test_expired_unsent_change_is_reprepared_without_reupload(env, monkeypatch):
    task = prepare(env)
    original = studio.commit
    def expired(account, key, body):
        with Session(get_engine()) as session:
            row = session.get(PublishChange, key)
            row.created_at -= timedelta(minutes=11)
            session.add(row); session.commit()
        return original(account, key, body)
    monkeypatch.setattr(studio, 'commit', expired)
    assert start(task)['status'] == 'failed'
    assert len(env['uploads']) == 1 and not env['writes']
    monkeypatch.setattr(studio, 'commit', original)
    assert start(task, 'retry')['status'] == 'completed'
    assert len(env['uploads']) == 1 and len(env['writes']) == 1


def test_corrupt_frozen_plan_is_rejected_before_queue_or_upload(env):
    task = prepare(env)
    with Session(get_engine()) as session:
        row = session.get(PublicationUpdate, task['id'])
        snapshot = json.loads(row.plan)
        snapshot['values']['description'] = '意外变化'
        row.plan = json.dumps(snapshot)
        session.add(row); session.commit()
    with pytest.raises(DubForgeError) as error:
        start(task)
    assert error.value.code == 'PUBLISH_CONFLICT'
    assert not env['uploads'] and not env['writes']


def test_plan_rejects_invalid_part_mapping_and_empty_scope(env):
    for body in [{}, {'replacements': [{'old_cid': 99999, 'artifact_id': env['artifacts'][0].id}]},
                 {'replacements': [{'old_cid': 1000, 'artifact_id': {}}]},
                 {'values': {'visibility': 'public'}}]:
        with pytest.raises(DubForgeError):
            updates.plan(env['source']['id'], body)


def test_api_context_and_plan_expose_safe_readable_snapshot_and_confirmation_guard(env):
    from app.main import app
    client = TestClient(app)
    base = '/api/publishing/publications/' + env['source']['id'] + '/updates'
    response = client.get(base + '/context')
    assert response.status_code == 200, response.text
    context = response.json()
    assert context['source']['bvid'] == 'BV1234567890' and context['metadata']['seasons'][0]['title'] == '越南'
    response = client.post(base + '/plan', json={'values': {'description': 'API新版简介'}})
    assert response.status_code == 200, response.text
    task = response.json()
    response = client.post('/api/publishing/updates/' + task['id'] + '/actions', json={'action': 'start'})
    assert response.status_code == 400 and response.json()['code'] == 'PUBLISH_UPDATE_CONFIRM'
    assert not env['writes'] and not env['uploads']
