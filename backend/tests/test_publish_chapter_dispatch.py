"""章节后台交付与投稿分离；平台和 LLM 全部使用替身。"""
import json
import time

import pytest
from sqlmodel import Session

from dubforge import configuration, workflow_plan, workflows
from dubforge.db import get_engine
from dubforge.publishing import chapter_dispatch, service
from dubforge.publishing.models import Publication
from test_chapter_sync import env, publication as chapter_publication, edit as edit_chapter_publication, saved


def publication(key='pub', *, enabled=True, state=None, **fields):
    return Publication(id=key, project_uid='project', status='published',
        snapshot=json.dumps({'chapter_options': {'enabled': enabled, 'items': [{'title': '开场', 'start_time': 0}]}}),
        chapter_sync=json.dumps(state or {}), **fields)


def test_scan_only_schedules_approved_explicitly_enabled_due_records(isolated_env, monkeypatch):
    from dubforge.publishing.models import now
    rows = [publication('new'), publication('off', enabled=False),
            publication('unknown', state={'status': 'unknown'}), publication('failed', state={'status': 'failed'}),
            publication('done', state={'status': 'completed'}),
            publication('due', state={'status': 'pending', 'next_check': 0}),
            publication('later', state={'status': 'pending', 'next_check': time.time() + 3600}),
            publication('archived', archived_at=now()), publication('deleted', deleted_at=now())]
    unapproved = publication('reviewing'); unapproved.status = 'submitted'; rows.append(unapproved)
    with Session(get_engine()) as session:
        session.add_all(rows); session.commit()
    calls = []
    monkeypatch.setattr(chapter_dispatch, '_closing', False)
    monkeypatch.setattr(chapter_dispatch, '_enqueue', lambda key: calls.append(key))
    chapter_dispatch.scan()
    assert set(calls) == {'new', 'due'}


def test_scan_does_not_run_while_closing_or_during_status_worker(isolated_env, monkeypatch):
    with Session(get_engine()) as session:
        session.add(publication()); session.commit()
    calls = []
    monkeypatch.setattr(chapter_dispatch, '_enqueue', lambda key: calls.append(key))
    monkeypatch.setattr(chapter_dispatch, '_closing', True)
    chapter_dispatch.scan()
    monkeypatch.setattr(chapter_dispatch, '_closing', False)
    monkeypatch.setitem(service._running, 'pub', object())
    chapter_dispatch.scan()
    assert calls == []


def test_one_damaged_record_does_not_stop_other_chapter_jobs(isolated_env, monkeypatch):
    broken = publication('broken'); broken.chapter_sync = '{'
    bad_shape = publication('bad-shape'); bad_shape.snapshot = '[]'
    with Session(get_engine()) as session:
        session.add_all([broken, bad_shape, publication('good')]); session.commit()
    calls = []
    monkeypatch.setattr(chapter_dispatch, '_closing', False)
    monkeypatch.setattr(chapter_dispatch, '_enqueue', lambda key: calls.append(key))
    chapter_dispatch.scan()
    assert calls == ['good']


def test_manual_submission_is_checked_but_workflow_owned_receipts_are_not_duplicated(isolated_env, monkeypatch):
    from dubforge.workflow_models import WorkflowRun
    manual = publication('manual'); manual.status = 'submitted'; manual.receipt = '{"bvid":"BV1234567890"}'
    owned = publication('owned'); owned.status = 'submitted'; owned.receipt = manual.receipt
    with Session(get_engine()) as session:
        session.add_all([manual, owned, WorkflowRun(id='flow', request_id='request', project_uid='project',
            publication_id='owned', status='monitoring')]); session.commit()
    calls = []
    monkeypatch.setattr(chapter_dispatch, '_closing', False)
    monkeypatch.setattr(chapter_dispatch, '_enqueue', lambda key: calls.append(key))
    chapter_dispatch.scan()
    assert calls == ['manual']


def test_review_check_is_read_only_and_throttled(isolated_env, monkeypatch):
    row = publication(); row.status = 'submitted'
    with Session(get_engine()) as session:
        session.add(row); session.commit()
    calls = []
    monkeypatch.setattr(service, 'enqueue', lambda key, body: calls.append((key, body)))
    chapter_dispatch._check_review('pub')
    assert calls == [('pub', {'action': 'check', 'revision': 1})]
    with Session(get_engine()) as session:
        stored = session.get(Publication, 'pub')
        state = json.loads(stored.chapter_sync)
        assert stored.status == 'submitted'
        assert state['next_check'] > time.time() + 50


@pytest.mark.parametrize('status', ['submitted', 'published'])
def test_stopped_workflow_is_not_reclassified_as_manual_background_work(isolated_env, monkeypatch, status):
    from dubforge.workflow_models import WorkflowRun
    row = publication('stopped'); row.status = status; row.receipt = '{"bvid":"BV1234567890"}'
    with Session(get_engine()) as session:
        session.add(row)
        session.add(WorkflowRun(id='flow', request_id='request', project_uid='project', publication_id='stopped',
            status='attention', post_processing_paused=True))
        session.commit()
    calls = []
    monkeypatch.setattr(chapter_dispatch, '_closing', False)
    monkeypatch.setattr(chapter_dispatch, '_enqueue', lambda key: calls.append(key))
    monkeypatch.setattr(service, 'enqueue', lambda *args: calls.append(args))
    chapter_dispatch.scan()
    chapter_dispatch._check_review('stopped')
    assert calls == []


@pytest.mark.parametrize('body', [{}, {'confirmed': False}, {'confirmed': True, 'enable': 1},
    {'confirmed': True, 'other': True}, {'confirmed': True, 'enable': True, 'reconcile_only': True}])
def test_retry_requires_narrow_explicit_confirmation(body):
    with pytest.raises(service.PublishError):
        chapter_dispatch.request('pub', body)


def test_unknown_request_only_enqueues_read_only_reconciliation(isolated_env, monkeypatch):
    from dubforge.publishing import chapter_sync
    row = publication(state={'status': 'unknown'})
    with Session(get_engine()) as session:
        session.add(row); session.commit()
    data = {'status': 'published', 'chapter_sync': {'status': 'unknown'}}
    monkeypatch.setattr(service, 'view', lambda key: data)
    monkeypatch.setattr(chapter_dispatch, '_closing', False)
    calls = []
    monkeypatch.setattr(chapter_sync, 'reset_for_retry', lambda *args, **kwargs: pytest.fail('未知结果不能重置'))
    monkeypatch.setattr(chapter_dispatch, '_enqueue', lambda key, **kwargs: calls.append((key, kwargs)))
    assert chapter_dispatch.request('pub', {'confirmed': True, 'reconcile_only': True}) is data
    assert calls == [('pub', {'reconcile_only': True})]


def test_enqueue_reserves_once_without_waiting_for_network(monkeypatch):
    calls = []
    monkeypatch.setattr(chapter_dispatch, '_closing', False)
    monkeypatch.setattr(chapter_dispatch, '_running', set())
    monkeypatch.setattr(chapter_dispatch._pool, 'submit', lambda *args: calls.append(args))
    assert chapter_dispatch._enqueue('pub') is True
    assert chapter_dispatch._enqueue('pub') is False
    assert len(calls) == 1


def test_monitor_waits_for_chapters_and_reports_failure_without_resubmission(monkeypatch):
    calls = []
    monkeypatch.setattr(workflows, '_update', lambda key, **fields: calls.append(fields))
    pub = {'status': 'published', 'snapshot': {'chapter_options': {'enabled': True, 'items': [1]}},
           'chapter_sync': {'status': 'running'}}
    assert workflows._finish_publication_monitor('flow', pub) is True
    assert calls[-1]['phase'] == '视频已发布，章节处理中'
    pub['chapter_sync'] = {'status': 'unknown', 'error': '写入结果待核对'}
    workflows._finish_publication_monitor('flow', pub)
    assert calls[-1]['status'] == 'attention'
    assert calls[-1]['error'] == '写入结果待核对'
    pub['chapter_sync'] = {'status': 'completed'}
    workflows._finish_publication_monitor('flow', pub)
    assert calls[-1]['status'] == 'completed'


def test_new_recipe_freezes_chapter_switches_and_historical_recipe_does_not_enable_them(make_project):
    uid = make_project()
    spec = configuration.run_spec(uid)
    assert spec['chapters_enabled'] is True
    assert spec['chapters_translate'] is True
    assert workflow_plan.recipe(spec)['chapters_enabled'] is True
    assert 'chapters_enabled' not in workflow_plan.recipe({'mode': 'produce'})
    with pytest.raises(workflow_plan.WorkflowError, match='章节开关'):
        workflow_plan.recipe({'chapters_enabled': 'true'})


def test_chapter_config_switches_are_strict_and_inherit(make_project):
    from test_configuration import save
    uid = make_project()
    save('project:' + uid, {'publish.chapters_enabled': False, 'publish.chapters_translate': False})
    spec = configuration.run_spec(uid)
    assert spec['chapters_enabled'] is False
    assert spec['chapters_translate'] is False
    current = configuration.resolve(uid)['values']
    with pytest.raises(configuration.ConfigurationError, match='章节开关'):
        configuration._validate({'publish.chapters_enabled': 1}, current)


def test_retry_api_completes_both_parts_without_upload_or_resubmission(env):
    from fastapi.testclient import TestClient
    from app.main import app
    key = chapter_publication(env, enabled=False)
    snapshot = json.loads(saved(key).snapshot)
    snapshot.update(settings=dict(service.DEFAULTS), origins={})
    edit_chapter_publication(key, snapshot=service._json(snapshot))
    with TestClient(app) as client:
        result = client.post(f'/api/publishing/publications/{key}/chapters/retry',
                             json={'confirmed': True, 'enable': True})
        assert result.status_code == 200, result.text
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = client.get(f'/api/publishing/publications/{key}').json()
            if value['chapter_sync'].get('status') == 'completed':
                break
            time.sleep(.01)
        assert value['chapter_sync']['status'] == 'completed'
        assert value['status'] == 'published'
        assert value['receipt']['bvid'] == 'BV1234567890'
        assert len(env.translated) == 1
        assert [payload['cid'] for operation, payload in env.calls if operation == 'studio_chapter_write'] == [1, 2]
        assert not any(operation in {'upload', 'submit', 'collection'} for operation, _ in env.calls)


def test_existing_database_adds_chapter_state_without_changing_publication(isolated_env):
    from dubforge import db
    row = publication(enabled=False)
    row.receipt = '{"bvid":"BV1234567890","aid":10}'
    with Session(get_engine()) as session:
        session.add(row); session.commit()
    with get_engine().begin() as connection:
        connection.exec_driver_sql('ALTER TABLE publication DROP COLUMN chapter_sync')
    db._ensure_columns()
    with Session(get_engine()) as session:
        restored = session.get(Publication, 'pub')
        assert restored.status == 'published'
        assert json.loads(restored.receipt) == {'bvid': 'BV1234567890', 'aid': 10}
        assert restored.chapter_sync == '{}'
