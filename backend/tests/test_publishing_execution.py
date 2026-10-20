"""发布诊断不能变成上传证据、重投授权或停止后的新副作用。"""
import json
import uuid

from sqlmodel import Session

from dubforge import workflows
from dubforge.db import get_engine
from dubforge.execution_progress import Recorder
from dubforge.publishing import service, transport
from dubforge.publishing.models import Publication
from dubforge.workflow_models import WorkflowRun
from test_publishing import setup_publish, finish, upload


def test_upload_microstages_preserve_real_byte_progress_and_safe_history(setup_publish, monkeypatch):
    _, row, _, calls = setup_publish
    original = transport.call
    def remote(operation, payload=None, **kwargs):
        if operation == 'upload':
            kwargs['on_event'](dict(event='progress', phase='preparing', loaded=0))
            kwargs['on_event'](dict(event='progress', phase='uploading', loaded=payload['size'] / 2))
            kwargs['on_event'](dict(event='progress', phase='merging', loaded=payload['size']))
            value = service.view(row['id'])
            assert value['execution']['phase'] == 'publish.upload.merging'
            assert value['execution']['unit'] == '字节'
            assert value['progress_detail']['phase'] == 'merging'
        return original(operation, payload, **kwargs)
    monkeypatch.setattr(transport, 'call', remote)
    result = upload(row)
    assert result['status'] == 'uploaded' and result['progress'] == 100
    assert result['execution']['state'] == 'completed'
    phases = {item['phase'] for item in result['execution']['history']}
    assert {'publish.files.verify', 'publish.upload.preparing', 'publish.upload.uploading',
            'publish.upload.merging', 'publish.cover.upload'} <= phases
    assert len(result['execution']['history']) <= 24
    text = json.dumps(result['execution'], ensure_ascii=False)
    assert 'encrypted-fixture' not in text and 'remote-' not in text and 'path' not in text
    assert not any(action == 'submit' for action, _ in calls)


def test_stopping_during_submission_keeps_bv_but_does_not_send_following_collection(setup_publish, monkeypatch):
    uid, row, _, calls = setup_publish
    with Session(get_engine()) as session:
        flow = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), publication_id=row['id'], status='monitoring')
        session.add(flow); session.commit(); session.refresh(flow)
        flow_id = flow.id
    original = transport.call
    def stop_after_accept(operation, payload=None, **kwargs):
        result = original(operation, payload, **kwargs)
        if operation == 'submit':
            workflows.stop(flow_id)
        return result
    monkeypatch.setattr(transport, 'call', stop_after_accept)
    preview = service.plan(row['id'])
    service.enqueue(row['id'], dict(action='publish', confirmed=True, revision=row['revision'],
                                   fingerprint=preview['fingerprint']))
    result = finish(row['id'])
    assert result['status'] == 'collection_pending' and result['receipt']['bvid'] == 'BV1234567890'
    assert len([action for action, _ in calls if action == 'submit']) == 1
    assert not any(action == 'collection' for action, _ in calls)
    assert workflows.post_processing_paused(row['id'])
    assert result['execution']['state'] == 'attention'


def test_diagnostic_only_transfer_record_does_not_prevent_recycling_unuploaded_draft(setup_publish):
    _, row, _, _ = setup_publish
    execution = Recorder(owner='publication', run_id=row['id']).phase('publish.preflight', '核验发布资料')
    service._update(row['id'], execution=execution)
    record = service.view(row['id'])
    assert record['lifecycle_actions']['delete']['allowed']
    result = service.manage_record(row['id'], 'delete', {'revision': row['revision'], 'confirmed': True})
    assert result['lifecycle'] == 'deleted'
    with Session(get_engine()) as session:
        saved = session.get(Publication, row['id'])
        assert set(json.loads(saved.transfers)) == {'_progress'}


def test_published_execution_and_chapter_execution_have_distinct_owners(setup_publish):
    _, row, _, _ = setup_publish
    with Session(get_engine()) as session:
        stored = session.get(Publication, row['id'])
        snapshot = dict(account_id='123', settings=dict(service.DEFAULTS), origins={}, parts=[],
                        chapter_options=dict(enabled=True, translate=True, permanent=True,
                                             items=[dict(title='开场', start_time=0, end_time=10)]))
        stored.snapshot = json.dumps(snapshot)
        stored.status = 'published'
        stored.chapter_sync = json.dumps(dict(schema=1, status='running', phase='翻译章节标题', parts=[]))
        session.add(stored); session.commit()
    record = service.view(row['id'])
    assert record['execution']['owner'] == 'publication' and record['execution']['state'] == 'completed'
    assert record['chapter_sync']['execution']['owner'] == 'chapters'
    assert record['chapter_sync']['execution']['state'] == 'running'
