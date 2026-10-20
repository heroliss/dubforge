"""顺序发布与本地收纳共用稿件身份；隔离数据库，不读取或写入B站。"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.main import app
from dubforge import release_batches
from dubforge.db import get_engine
from dubforge.errors import DubForgeError
from dubforge.publishing import service
from dubforge.publishing.models import Publication
from dubforge.release_batch_models import ReleaseBatch
from dubforge.workflow_models import WorkflowRun


def _manage(client, record, action):
    body = dict(revision=record['revision'], confirmed=True)
    path = '/api/publishing/publications/' + record['id']
    if action == 'delete':
        return client.request('DELETE', path, json=body)
    return client.post(path + '/' + action, json=body)


def test_archived_remote_receipt_remains_a_batch_candidate(make_project):
    uid = make_project()
    record = service.create(uid)
    with Session(get_engine()) as session:
        row = session.get(Publication, record['id'])
        row.status = 'published'
        row.snapshot = json.dumps(dict(account_id='123', settings={**service.DEFAULTS, 'title': '原稿'}, origins={}))
        row.receipt = json.dumps(dict(aid=12345, bvid='BV0000012345', collection_joined=True))
        session.add(row); session.commit()
    client = TestClient(app)
    result = _manage(client, service.view(record['id']), 'archive')
    assert result.status_code == 200, result.text
    assert client.get('/api/publishing/publications').json() == []
    archived = client.get('/api/publishing/publications?lifecycle=archived').json()
    assert archived[0]['receipt']['bvid'] == 'BV0000012345'
    assert service.project_status(uid)['id'] == record['id']
    candidate = release_batches.candidates([uid])['items'][0]
    assert candidate['publication_id'] == record['id']
    assert candidate['publication_choices'][0]['bvid'] == 'BV0000012345'
    assert not candidate['blockers']


def test_recycled_draft_is_excluded_but_keeps_its_original_identity(make_project):
    uid = make_project()
    record = service.create(uid)
    client = TestClient(app)
    result = _manage(client, record, 'delete')
    assert result.status_code == 200, result.text
    candidate = release_batches.candidates([uid])['items'][0]
    assert not candidate['publication_id']
    assert not candidate['publication_choices']
    with pytest.raises(DubForgeError):
        release_batches.candidates([uid], {uid: record['id']})
    retained = client.get('/api/publishing/publications?lifecycle=deleted').json()
    assert retained[0]['id'] == record['id']
    assert retained[0]['draft'] == record['draft']
    restored = _manage(client, retained[0], 'restore')
    assert restored.status_code == 200
    assert release_batches.candidates([uid])['items'][0]['publication_id'] == record['id']


def test_active_batch_protects_a_draft_from_recycling(make_project):
    uid = make_project()
    record = service.create(uid)
    with Session(get_engine()) as session:
        batch = ReleaseBatch(request_id='record-protection', status='running',
                             items=json.dumps([dict(project_uid=uid, publication_id=record['id'], status='queued')]))
        session.add(batch); session.commit()
    response = _manage(TestClient(app), record, 'delete')
    assert response.status_code == 400, response.text
    assert response.json()['code'] == 'PUBLISH_RECORD_BUSY'
    retained = service.view(record['id'])
    assert retained['lifecycle'] == 'active'
    assert retained['draft'] == record['draft']


def test_archiving_a_draft_does_not_allow_old_files_to_publish(make_project):
    uid = make_project()
    record = service.create(uid)
    archived = _manage(TestClient(app), record, 'archive')
    assert archived.status_code == 200
    with pytest.raises(DubForgeError) as error:
        service.plan(record['id'])
    assert error.value.code == 'PUBLISH_RECORD_INACTIVE'
    assert service.view(record['id'])['draft'] == record['draft']


def test_retained_workflow_reference_to_recycled_draft_requires_restore(make_project):
    uid = make_project()
    record = service.create(uid)
    with Session(get_engine()) as session:
        session.add(WorkflowRun(request_id='retained-reference', project_uid=uid,
                                publication_id=record['id'], status='paused'))
        session.commit()
    response = _manage(TestClient(app), record, 'delete')
    assert response.status_code == 200, response.text
    candidate = release_batches.candidates([uid])['items'][0]
    assert candidate['publication_id'] == record['id']
    assert any('回收站' in message and '恢复' in message for message in candidate['blockers'])
    assert not candidate['publication_choices']
    assert len(service.list_publications(uid, lifecycle_filter='all')) == 1
