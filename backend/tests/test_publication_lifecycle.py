"""本地发布记录回收与归档：隔离数据库，不请求B站、不移除媒体或远端证据。"""
import json
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from dubforge import db, workflows, release_batches
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.project_archive import set_archived
from dubforge.publishing import service, studio, review_draft, transport
from dubforge.publishing.models import Publication, PublishChange
from dubforge.release_batch_models import ReleaseBatch
from dubforge.workflow_models import WorkflowRun


@pytest.fixture()
def draft(make_project, monkeypatch):
    monkeypatch.setattr(transport, 'call', lambda *args, **kwargs: pytest.fail('本地生命周期不得请求B站'))
    uid = make_project()
    return service.create(uid)


def manage(row, action, **updates):
    return service.manage_record(row['id'], action, dict(revision=row['revision'], confirmed=True, **updates))


def stored(key):
    with Session(db.get_engine()) as session:
        return session.get(Publication, key)


def update(row, **changes):
    with Session(db.get_engine()) as session:
        value = session.get(Publication, row['id'])
        for key, data in changes.items():
            setattr(value, key, service._json(data) if key in {'snapshot', 'transfers', 'receipt', 'draft'} else data)
        session.add(value)
        session.commit()
    return service.view(row['id'])


def frozen():
    return dict(account_id='123', settings={**service.DEFAULTS, 'title': '保留原标题'},
                origins={}, parts=[], cover_sha256='isolated', publish_after_upload=True)


def test_draft_recycle_is_restorable_and_keeps_exact_payload_and_original_id(draft):
    before = stored(draft['id'])
    recycled = manage(draft, 'delete')
    assert recycled['lifecycle'] == 'deleted' and recycled['deleted_at'].endswith('+00:00')
    assert recycled['archived_at'] is None
    assert service.list_publications() == []
    assert service.list_publications(lifecycle_filter='deleted')[0]['id'] == draft['id']
    assert service.list_publications(lifecycle_filter='all')[0]['id'] == draft['id']
    assert service.project_status(draft['project_uid'])['id'] is None
    after = stored(draft['id'])
    for key in ('draft', 'snapshot', 'transfers', 'receipt', 'status', 'created_at', 'project_uid'):
        assert getattr(after, key) == getattr(before, key)
    restored = manage(recycled, 'restore')
    assert restored['id'] == draft['id'] and restored['lifecycle'] == 'active'
    assert restored['revision'] == draft['revision'] + 2
    assert restored['deleted_at'] is None
    assert service.list_publications()[0]['id'] == draft['id']
    assert service.list_publications(lifecycle_filter='deleted') == []


@pytest.mark.parametrize('status', ['uploaded', 'paused', 'failed', 'submitted', 'published',
                                  'rejected', 'private', 'reviewed', 'collection_pending'])
def test_archive_preserves_remote_identity_and_project_publication_state(draft, status):
    record = update(draft, status=status, snapshot=frozen(), transfers={'1': {'cid': 55, 'filename': 'upload-id'}},
                    receipt={'bvid': 'BV1234567890', 'aid': 222, 'collection_joined': False}, progress=100)
    before = stored(record['id'])
    archived = manage(record, 'archive')
    assert archived['lifecycle'] == 'archived' and archived['status'] == status
    assert service.list_publications(record['project_uid']) == []
    assert service.list_publications(record['project_uid'], lifecycle_filter='archived')[0]['id'] == record['id']
    assert service.project_status(record['project_uid'])['id'] == record['id']
    assert service.project_statuses([record['project_uid']])[record['project_uid']]['status'] == status
    with Session(db.get_engine()) as session:
        assert session.exec(select(Publication).where(Publication.project_uid == record['project_uid'])).one().id == record['id']
    after = stored(record['id'])
    for key in ('draft', 'snapshot', 'transfers', 'receipt', 'status', 'progress', 'created_at'):
        assert getattr(after, key) == getattr(before, key)
    assert not archived['lifecycle_actions']['delete']['allowed']
    with pytest.raises(DubForgeError) as exc:
        manage(archived, 'delete')
    assert exc.value.code == 'PUBLISH_RECORD_PROTECTED'
    restored = manage(archived, 'restore')
    assert restored['receipt']['bvid'] == 'BV1234567890' and restored['snapshot']['account_id'] == '123'


def test_project_status_prefers_active_publication_over_newer_archived_record(draft):
    # 归档旧稿会刷新 updated_at；列表摘要仍必须指向同项目的活动稿。
    active = service.create(draft['project_uid'])
    manage(draft, 'archive')
    statuses = service.project_statuses([draft['project_uid']])
    assert statuses[draft['project_uid']]['id'] == active['id']
    assert statuses[draft['project_uid']]['lifecycle'] == 'active'

    # 没有活动稿时，继续回退到最新的未删除归档记录。
    archived_active = manage(active, 'archive')
    statuses = service.project_statuses([draft['project_uid']])
    assert statuses[draft['project_uid']]['id'] == archived_active['id']
    assert statuses[draft['project_uid']]['lifecycle'] == 'archived'


@pytest.mark.parametrize('evidence', [dict(status='failed'), dict(status='uploaded'),
    dict(receipt={'bvid': 'BV1234567890'}), dict(receipt={'aid': 55}),
    dict(transfers={'1': {'cid': 55}}), dict(snapshot=frozen())])
def test_recycle_rejects_any_uploaded_or_submission_evidence(draft, evidence):
    record = update(draft, **evidence)
    assert not record['lifecycle_actions']['delete']['allowed']
    with pytest.raises(DubForgeError) as exc:
        manage(record, 'delete')
    assert exc.value.code == 'PUBLISH_RECORD_PROTECTED'
    assert stored(record['id']).deleted_at is None


@pytest.mark.parametrize('state', ['archive', 'delete'])
def test_hidden_records_are_readable_but_no_write_or_publish_entry_can_bypass_restore(draft, state):
    record = manage(draft, state)
    key = record['id']
    assert service.view(key)['lifecycle'] in {'archived', 'deleted'}
    operations = [lambda: service.save(key, dict(record['draft'], revision=record['revision'])),
        lambda: service.copy_draft(key), lambda: service.plan(key),
        lambda: service.enqueue(key, dict(action='upload', revision=record['revision'])),
        lambda: service.suggest(key, dict(revision=record['revision'])),
        lambda: review_draft.plan(key)]
    for operation in operations:
        with pytest.raises(DubForgeError) as exc:
            operation()
        assert exc.value.code == 'PUBLISH_RECORD_INACTIVE'
    with Session(db.get_engine()) as session:
        with pytest.raises(DubForgeError) as exc:
            studio._uploaded(session, key, '123')
        assert exc.value.code == 'PUBLISH_RECORD_INACTIVE'
        assert len(session.exec(select(Publication)).all()) == 1


@pytest.mark.parametrize('status', sorted(service.ACTIVE | {'preparing', 'stopping', 'unknown'}))
@pytest.mark.parametrize('action', ['archive', 'delete'])
def test_runtime_and_uncertain_states_cannot_be_hidden(draft, status, action):
    record = update(draft, status=status)
    assert not record['lifecycle_actions'][action]['allowed']
    with pytest.raises(DubForgeError) as exc:
        manage(record, action)
    assert exc.value.code == 'PUBLISH_RECORD_BUSY'
    assert stored(record['id']).deleted_at is None and stored(record['id']).archived_at is None


@pytest.mark.parametrize('kind', ['upload_cleanup', 'ai', 'workflow_lease', 'batch_lease'])
def test_in_memory_ownership_still_blocks_after_database_status_is_terminal(draft, kind):
    marker = {'upload_cleanup': service._running, 'workflow_lease': workflows._leases,
              'batch_lease': release_batches._leases}.get(kind)
    key = draft['id'] if kind == 'upload_cleanup' else draft['project_uid']
    if kind == 'ai':
        service._suggesting.add(draft['id'])
    else:
        marker[key] = threading.Event() if kind == 'upload_cleanup' else 'isolated-owner'
    try:
        with pytest.raises(DubForgeError) as exc:
            manage(draft, 'delete')
        assert exc.value.code == 'PUBLISH_RECORD_BUSY'
    finally:
        if kind == 'ai':
            service._suggesting.discard(draft['id'])
        else:
            marker.pop(key, None)


@pytest.mark.parametrize('status', ['queued', 'running', 'stopping', 'monitoring'])
def test_open_workflow_reference_blocks_without_relying_on_memory_lease(draft, status):
    with Session(db.get_engine()) as session:
        session.add(WorkflowRun(request_id=str(uuid.uuid4()), project_uid=draft['project_uid'],
                                publication_id=draft['id'], status=status))
        session.commit()
    with pytest.raises(DubForgeError) as exc:
        manage(draft, 'archive')
    assert exc.value.code == 'PUBLISH_RECORD_BUSY'


@pytest.mark.parametrize('status', sorted(release_batches.ACTIVE))
def test_active_batch_reference_blocks_waiting_publication(draft, status):
    with Session(db.get_engine()) as session:
        session.add(ReleaseBatch(request_id=str(uuid.uuid4()), status=status,
            items=service._json([dict(project_uid=draft['project_uid'], publication_id=draft['id'])])))
        session.commit()
    with pytest.raises(DubForgeError) as exc:
        manage(draft, 'delete')
    assert exc.value.code == 'PUBLISH_RECORD_BUSY'


@pytest.mark.parametrize('status', ['prepared', 'paused', 'failed', 'done'])
def test_inactive_workflow_reference_is_preserved_and_can_be_restored(draft, status):
    with Session(db.get_engine()) as session:
        session.add(WorkflowRun(request_id=str(uuid.uuid4()), project_uid=draft['project_uid'],
                                publication_id=draft['id'], status=status))
        session.commit()
    record = manage(draft, 'delete')
    with Session(db.get_engine()) as session:
        assert session.exec(select(WorkflowRun)).one().publication_id == draft['id']
    assert manage(record, 'restore')['id'] == draft['id']


@pytest.mark.parametrize('status', ['committing', 'unknown'])
@pytest.mark.parametrize('reference', ['direct', 'same_remote_identity'])
def test_remote_change_inflight_and_unknown_identity_guard_survive_archive(draft, status, reference):
    record = update(draft, status='published', snapshot=frozen(), receipt={'bvid': 'BV1234567890', 'aid': 222})
    with Session(db.get_engine()) as session:
        session.add(PublishChange(account_id='123', bvid='BV1234567890', kind='append', status=status,
                                  publication_id=record['id'] if reference == 'direct' else ''))
        session.commit()
    with pytest.raises(DubForgeError) as exc:
        manage(record, 'archive')
    assert exc.value.code == 'PUBLISH_RECORD_BUSY'


def test_project_task_block_and_archived_project_local_management(draft):
    uid = draft['project_uid']
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'tts')
    step.status = 'queued'
    crud.save_step(step)
    with pytest.raises(DubForgeError) as exc:
        manage(draft, 'delete')
    assert exc.value.code == 'PUBLISH_RECORD_BUSY'
    step.status = 'pending'
    crud.save_step(step)
    set_archived(uid, True)
    archived = manage(draft, 'archive')
    restored = manage(archived, 'restore')
    assert crud.get_project(uid).archived_at is not None
    assert restored['lifecycle'] == 'active'
    with pytest.raises(DubForgeError) as exc:
        service.save(restored['id'], dict(restored['draft'], revision=restored['revision']))
    assert exc.value.code == 'PROJECT_ARCHIVED'


def test_orphan_draft_remains_readable_and_recoverable(draft):
    record = update(draft, project_uid='missing-isolated-project')
    assert record['settings']['title'] == draft['draft']['content'].get('title', '')
    recycled = manage(record, 'delete')
    assert recycled['lifecycle'] == 'deleted'
    assert manage(recycled, 'restore')['id'] == record['id']
    with pytest.raises(DubForgeError):
        service.copy_draft(record['id'])


def test_revision_confirmation_filter_and_idempotent_current_target(draft):
    for body in ({'revision': draft['revision']}, {'revision': draft['revision'], 'confirmed': False},
                 {'revision': draft['revision'], 'confirmed': True, 'remote': True}):
        with pytest.raises(DubForgeError) as exc:
            service.manage_record(draft['id'], 'archive', body)
        assert exc.value.code == 'PUBLISH_RECORD_CONFIRM'
    record = manage(draft, 'archive')
    with pytest.raises(DubForgeError) as exc:
        manage(draft, 'delete')
    assert exc.value.code == 'PUBLISH_CONFLICT'
    repeat = manage(record, 'archive')
    assert repeat['revision'] == record['revision'] and repeat['archived_at'] == record['archived_at']
    with pytest.raises(DubForgeError) as exc:
        service.list_publications(lifecycle_filter='incorrect')
    assert exc.value.code == 'PUBLISH_RECORD_FILTER'


def test_archived_remote_record_can_check_status_without_becoming_active_or_resubmitting(draft, monkeypatch):
    record = update(draft, status='submitted', snapshot=frozen(),
                    receipt={'bvid': 'BV1234567890', 'aid': 222})
    archived = manage(record, 'archive')
    calls = []
    monkeypatch.setattr(transport, 'available', lambda: True)
    monkeypatch.setattr(service, '_credential', lambda account: {'isolated': True})

    def fake_status(operation, payload, **kwargs):
        calls.append((operation, payload))
        assert operation == 'status' and payload['bvid'] == 'BV1234567890'
        with pytest.raises(DubForgeError) as exc:
            manage(service.view(record['id']), 'restore')
        assert exc.value.code == 'PUBLISH_RECORD_BUSY'
        return dict(state=0, state_desc='审核通过', videos=[])

    monkeypatch.setattr(transport, 'call', fake_status)
    service.enqueue(record['id'], dict(action='check', revision=archived['revision']))
    deadline = time.monotonic() + 5
    while record['id'] in service._running and time.monotonic() < deadline:
        time.sleep(.005)
    assert record['id'] not in service._running
    checked = service.view(record['id'])
    assert checked['status'] == 'published' and checked['lifecycle'] == 'archived'
    assert checked['archived_at'] == archived['archived_at']
    assert checked['revision'] == archived['revision']
    assert [operation for operation, _ in calls] == ['status']
    for action in ('upload', 'publish', 'submit', 'collection'):
        with pytest.raises(DubForgeError) as exc:
            service.enqueue(record['id'], dict(action=action, revision=archived['revision']))
        assert exc.value.code == 'PUBLISH_RECORD_INACTIVE'


def test_local_management_never_moves_or_changes_project_media(draft):
    path = crud.project_dir(draft['project_uid']) / 'artifacts' / 'assemble' / 'isolated-retained.mp4'
    path.write_bytes(b'isolated-media-bytes')
    before = path.stat()
    record = manage(draft, 'archive')
    record = manage(record, 'delete')
    manage(record, 'restore')
    assert path.read_bytes() == b'isolated-media-bytes'
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_http_lifecycle_contract_is_local_confirmed_and_same_origin(draft):
    from app.main import app
    client = TestClient(app)
    key = draft['id']
    root = f'/api/publishing/publications/{key}'
    response = client.request('DELETE', root, json={'revision': draft['revision'], 'confirmed': True})
    assert response.status_code == 200
    record = response.json()
    assert record['lifecycle'] == 'deleted'
    assert client.get('/api/publishing/publications').json() == []
    assert client.get('/api/publishing/publications?lifecycle=deleted').json()[0]['id'] == key
    assert client.get(root).json()['lifecycle'] == 'deleted'
    response = client.post(root + '/restore', json={'revision': record['revision'], 'confirmed': True})
    assert response.status_code == 200 and response.json()['lifecycle'] == 'active'
    record = response.json()
    blocked = client.post(root + '/archive', json={'revision': record['revision'], 'confirmed': True},
                          headers={'Origin': 'https://unrelated.example', 'Sec-Fetch-Site': 'cross-site'})
    assert blocked.status_code == 400 and blocked.json()['code'] == 'PUBLISH_ORIGIN'
    assert service.view(key)['lifecycle'] == 'active'


def test_old_publication_schema_migration_defaults_to_active_and_keeps_payload(draft):
    with db.get_engine().connect() as connection:
        connection.exec_driver_sql('DROP INDEX IF EXISTS ix_publication_archived_at')
        connection.exec_driver_sql('DROP INDEX IF EXISTS ix_publication_deleted_at')
        connection.exec_driver_sql('ALTER TABLE publication DROP COLUMN archived_at')
        connection.exec_driver_sql('ALTER TABLE publication DROP COLUMN deleted_at')
        connection.commit()
    db._ensure_columns()
    db._ensure_columns()
    record = service.view(draft['id'])
    assert record['lifecycle'] == 'active' and record['draft'] == draft['draft']
    assert record['archived_at'] is None and record['deleted_at'] is None
