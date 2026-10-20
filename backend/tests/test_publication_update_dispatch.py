"""新原稿更新与旧自动章节共用启动门禁；不改旧章节历史。"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlmodel import Session

from dubforge import runtime_edit, workflows
from dubforge.db import get_engine
from dubforge.publishing import chapter_dispatch as dispatch, chapter_sync, service, transport, updates
from dubforge.publishing.models import Publication, PublicationUpdate

BV = 'BV1234567890'


@pytest.fixture
def env(isolated_env, monkeypatch):
    monkeypatch.setattr(dispatch, '_closing', False)
    monkeypatch.setattr(dispatch, '_running', set())
    monkeypatch.setattr(updates, '_running', {})
    monkeypatch.setattr(service, '_running', {})
    monkeypatch.setattr(workflows, 'chapter_status_changed', lambda key: None)
    monkeypatch.setattr(transport, 'call', lambda *args, **kwargs: pytest.fail('调度测试禁止真实平台请求'))
    row = Publication(id='source', project_uid='project', status='published',
        snapshot=json.dumps(dict(account_id='123', chapter_options=dict(enabled=True,
            items=[dict(title='开场', start_time=0, end_time=30)]))),
        receipt=json.dumps(dict(bvid=BV, aid=10)), transfers='{"100":{"cid":1}}',
        chapter_sync=json.dumps(dict(schema=1, status='pending', next_check=0, parts=[dict(cid=1, status='pending')])))
    with Session(get_engine()) as session:
        session.add(row)
        session.commit()
    return 'source'


def update(*, key='update', source='source', status='waiting', edit='pending', replaced=True):
    snapshot = dict(source_revision=1, parts=[dict(artifact_id=100, replaced=replaced)])
    row = PublicationUpdate(id=key, source_publication_id=source, project_uid='project', account_id='123', bvid=BV,
        status=status, fingerprint=service._hash(snapshot), plan=json.dumps(snapshot),
        execution=json.dumps(dict(stages=dict(edit=dict(status=edit)))))
    with Session(get_engine()) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def stored():
    with Session(get_engine()) as session:
        row = session.get(Publication, 'source')
        return dict(snapshot=row.snapshot, receipt=row.receipt, transfers=row.transfers, chapter_sync=row.chapter_sync)


@pytest.mark.parametrize('status', ['queued', 'uploading', 'submitting', 'waiting', 'unknown', 'delivering'])
def test_scan_does_not_schedule_old_chapters_during_any_open_update(env, monkeypatch, status):
    update(status=status)
    before = stored()
    scheduled = []
    monkeypatch.setattr(dispatch, '_enqueue', lambda key, **kwargs: scheduled.append(key))
    dispatch.scan()
    assert not scheduled and stored() == before


def test_same_bv_update_from_another_local_record_also_blocks_old_chapters(env, monkeypatch):
    update(source='other-source')
    scheduled = []
    monkeypatch.setattr(dispatch, '_enqueue', lambda key, **kwargs: scheduled.append(key))
    dispatch.scan()
    assert not scheduled


@pytest.mark.parametrize('status', ['completed', 'failed', 'paused'])
def test_verified_replacement_takes_over_even_when_later_delivery_is_incomplete(env, monkeypatch, status):
    update(status=status, edit='completed')
    before = stored()
    scheduled = []
    monkeypatch.setattr(dispatch, '_enqueue', lambda key, **kwargs: scheduled.append(key))
    dispatch.scan()
    assert not scheduled and stored() == before
    with pytest.raises(service.PublishError, match='请在更新原稿件任务中继续章节') as error:
        dispatch.request('source', dict(confirmed=True))
    assert error.value.code == 'PUBLISH_CHAPTER_SUPERSEDED'
    assert stored() == before


def test_completed_text_only_update_does_not_supersede_original_video_axis(env, monkeypatch):
    update(status='completed', edit='completed', replaced=False)
    scheduled = []
    monkeypatch.setattr(dispatch, '_enqueue', lambda key, **kwargs: scheduled.append(key))
    dispatch.scan()
    assert scheduled == ['source']


def test_unsubmitted_cancelled_replacement_keeps_old_chapter_retry_available(env, monkeypatch):
    update(status='paused', edit='pending')
    scheduled = []
    monkeypatch.setattr(dispatch, '_enqueue', lambda key, **kwargs: scheduled.append(key))
    dispatch.scan()
    assert scheduled == ['source']


def test_manual_request_during_open_update_does_not_reset_old_state(env, monkeypatch):
    update(status='waiting')
    before = stored()
    monkeypatch.setattr(service, 'view', lambda key: dict(status='published', chapter_sync=dict(status='pending')))
    monkeypatch.setattr(chapter_sync, 'reset_for_retry', lambda *a, **k: pytest.fail('接管中不能重置旧章节'))
    with pytest.raises(service.PublishError, match='请在更新原稿件任务中继续章节') as error:
        dispatch.request('source', dict(confirmed=True, enable=True))
    assert error.value.code == 'PUBLISH_UPDATE_BUSY'
    assert stored() == before


def test_read_only_old_chapter_request_also_explains_new_video_ownership(env, monkeypatch):
    update(status='failed', edit='completed')
    monkeypatch.setattr(service, 'view', lambda key: dict(status='published', chapter_sync=dict(status='unknown')))
    with pytest.raises(service.PublishError) as error:
        dispatch.request('source', dict(confirmed=True, reconcile_only=True))
    assert error.value.code == 'PUBLISH_CHAPTER_SUPERSEDED'


def test_queued_old_worker_checks_again_before_reading_old_transfers(env, monkeypatch):
    update(status='completed', edit='completed')
    before = stored()
    dispatch._running.add('source')
    monkeypatch.setattr(chapter_sync, 'run', lambda *a, **k: pytest.fail('不应处理旧CID'))
    dispatch._execute('source')
    assert 'source' not in dispatch._running
    assert stored() == before


def test_legacy_submitted_review_check_is_not_started_after_replacement(env, monkeypatch):
    update(status='completed', edit='completed')
    with Session(get_engine()) as session:
        row = session.get(Publication, 'source')
        row.status = 'submitted'
        session.add(row)
        session.commit()
    before = stored()
    monkeypatch.setattr(service, 'enqueue', lambda *a, **k: pytest.fail('不应启动旧审核章节调度'))
    dispatch._check_review('source')
    assert stored() == before


def test_old_chapter_reservation_blocks_new_update_start_without_queueing_update(env, monkeypatch):
    row = update(status='prepared')
    calls = []
    monkeypatch.setattr(dispatch._pool, 'submit', lambda *args: calls.append(args))
    monkeypatch.setattr(updates._pool, 'submit', lambda *args: pytest.fail('旧章节占用中不能启动更新'))
    monkeypatch.setattr(runtime_edit, 'assert_editable', lambda *args, **kwargs: None)
    assert dispatch._enqueue('source') is True
    with pytest.raises(service.PublishError, match='章节正在执行'):
        updates.action(row.id, dict(action='start', confirmed=True, fingerprint=row.fingerprint))
    assert len(calls) == 1
    assert updates.view(row.id)['status'] == 'prepared'


def test_update_reservation_and_old_scan_share_one_gate_under_race(env, monkeypatch):
    row = update(status='prepared')
    update_reserved = threading.Event()
    release_update = threading.Event()
    scan_started = threading.Event()
    calls = []

    def submit_update(*args):
        update_reserved.set()
        assert release_update.wait(5)

    def scan():
        scan_started.set()
        dispatch.scan()

    monkeypatch.setattr(updates._pool, 'submit', submit_update)
    monkeypatch.setattr(dispatch._pool, 'submit', lambda *args: calls.append(args))
    monkeypatch.setattr(runtime_edit, 'assert_editable', lambda *args, **kwargs: None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        started = pool.submit(updates.action, row.id, dict(action='start', confirmed=True, fingerprint=row.fingerprint))
        assert update_reserved.wait(5)
        scanned = pool.submit(scan)
        assert scan_started.wait(5)
        release_update.set()
        assert started.result(timeout=5)['status'] == 'queued'
        scanned.result(timeout=5)
    assert not calls and 'source' not in dispatch._running
