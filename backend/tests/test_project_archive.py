"""归档管理字段、恢复清单和任务互斥；全部使用隔离数据目录。"""
import datetime
import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete

from app.main import app
from dubforge import db, runtime_edit, video_info, workflows
from dubforge.domain import crud
from dubforge.domain.models import Artifact, Job, Project, StepRun
from dubforge.pipeline import dubbing_session
from dubforge.project_archive import ProjectArchiveError, assert_active, set_archived
from dubforge.publishing import service, studio, project_copy
from dubforge.publishing.models import Publication, PublishChange
from dubforge.runner import compute_fingerprint, runner
from dubforge.workflow_models import WorkflowRun


def _remove_index(uid):
    """模拟索引丢失，保留目录和 manifest。"""
    project = crud.get_project(uid)
    with crud.session() as session:
        for model in (Artifact, Job, StepRun):
            session.exec(delete(model).where(model.project_id == project.id))
        session.exec(delete(Project).where(Project.id == project.id))
        session.commit()


def _publication(uid, status):
    with Session(db.get_engine()) as session:
        row = Publication(project_uid=uid, status=status)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def test_archive_restore_are_idempotent_and_keep_production_data(make_project):
    uid = make_project(params={'auto_steps': ['download'], 'custom': '保留设置'})
    project = crud.get_project(uid)
    project.status = 'done'
    crud.save_project(project)
    step = crud.get_step(project.id, 'download')
    step.status = 'done'
    step.input_fingerprint = '已完成制作指纹'
    crud.save_step(step)
    root = crud.project_dir(uid)
    path = root / 'media' / 'video.mp4'
    path.write_bytes(b'production media')
    crud.add_artifact(project.id, 'download', 'video', 'video.mp4', path, 'media/video.mp4')
    before_project = crud.get_project(uid)
    before_steps = [row.model_dump() for row in crud.get_steps(project.id)]
    before_artifacts = {key: value.model_dump() for key, value in crud.latest_artifacts(project.id, None).items()}
    before_fingerprint = compute_fingerprint(before_project, 'download')

    first = set_archived(uid, True)
    timestamp = first.archived_at
    assert timestamp is not None
    saved = crud.get_project(uid)
    manifest = (root / 'manifest.json').read_bytes()
    repeated = set_archived(uid, True)
    assert repeated.archived_at == timestamp
    assert crud.get_project(uid).updated_at == saved.updated_at
    assert (root / 'manifest.json').read_bytes() == manifest
    assert crud.project_dir(uid) == root
    assert path.read_bytes() == b'production media'
    assert saved.status == before_project.status
    assert saved.params == before_project.params
    assert saved.created_at == before_project.created_at
    assert [row.model_dump() for row in crud.get_steps(project.id)] == before_steps
    assert {key: value.model_dump() for key, value in crud.latest_artifacts(project.id, None).items()} == before_artifacts
    assert compute_fingerprint(saved, 'download') == before_fingerprint
    with pytest.raises(ProjectArchiveError) as error:
        assert_active(uid)
    assert error.value.code == 'PROJECT_ARCHIVED'
    assert '恢复' in error.value.suggestion

    restored = set_archived(uid, False)
    assert restored.archived_at is None
    updated_at = crud.get_project(uid).updated_at
    set_archived(uid, False)
    assert crud.get_project(uid).updated_at == updated_at
    assert assert_active(uid).uid == uid
    assert json.loads((root / 'manifest.json').read_text(encoding='utf-8'))['archived_at'] is None


def test_listing_preserves_domain_default_and_filters_api_default(make_project):
    active = make_project(title='活动项目')
    archived = make_project(title='保留已发布项目')
    set_archived(archived, True)
    assert {p.uid for p in crud.list_projects()} == {active, archived}
    assert {p.uid for p in crud.list_projects(archive='all')} == {active, archived}
    assert [p.uid for p in crud.list_projects(archive='active')] == [active]
    assert [p.uid for p in crud.list_projects(archive='archived')] == [archived]

    client = TestClient(app)
    assert [row['uid'] for row in client.get('/api/projects').json()] == [active]
    assert [row['uid'] for row in client.get('/api/projects?archive=active').json()] == [active]
    assert {row['uid'] for row in client.get('/api/projects?archive=all').json()} == {active, archived}
    assert [row['uid'] for row in client.get('/api/projects?archive=archived').json()] == [archived]
    for suffix in ('', '/navigation'):
        archived_view = client.get(f'/api/projects/{archived}{suffix}').json()
        active_view = client.get(f'/api/projects/{active}{suffix}').json()
        assert archived_view['archived'] is True and archived_view['archived_at']
        assert active_view['archived'] is False and active_view['archived_at'] is None


def test_api_archive_response_and_invalid_requests_have_stable_errors(make_project):
    uid = make_project()
    client = TestClient(app)
    response = client.put(f'/api/projects/{uid}/archive', json={'archived': True})
    assert response.status_code == 200
    first = response.json()
    assert first == dict(ok=True, uid=uid, archived=True,
        archived_at=crud.get_project(uid).archived_at.replace(tzinfo=datetime.timezone.utc).isoformat())
    assert client.put(f'/api/projects/{uid}/archive', json={'archived': True}).json() == first
    assert client.put(f'/api/projects/{uid}/archive', json={'archived': False}).json() == dict(
        ok=True, uid=uid, archived=False, archived_at=None)
    for invalid in (None, {}, [], {'archived': 'true'}, {'archived': 1}, {'archived': None},
                    {'archived': True, 'unexpected': 1}):
        response = client.put(f'/api/projects/{uid}/archive', json=invalid)
        assert response.status_code == 400
        assert response.json()['code'] == 'PROJECT_ARCHIVE_INVALID'
        assert '归档' in response.json()['message']
    response = client.put('/api/projects/missing/archive', json={'archived': True})
    assert response.status_code == 400
    assert response.json()['code'] == 'PROJECT_NOT_FOUND'
    response = client.get('/api/projects?archive=invalid')
    assert response.status_code == 400
    assert response.json()['code'] == 'PROJECT_ARCHIVE_FILTER_INVALID'
    with pytest.raises(ProjectArchiveError) as error:
        assert_active('missing')
    assert error.value.code == 'PROJECT_NOT_FOUND'
    with pytest.raises(ProjectArchiveError) as error:
        set_archived(uid, 'true')
    assert error.value.code == 'PROJECT_ARCHIVE_INVALID'


def test_existing_database_adds_nullable_archive_column_without_hiding_projects(make_project):
    uid = make_project(title='升级前的项目')
    with db.get_engine().connect() as connection:
        connection.exec_driver_sql('ALTER TABLE project DROP COLUMN archived_at')
        connection.commit()
    db._ensure_columns()
    db._ensure_columns()
    assert crud.get_project(uid).archived_at is None
    assert [p.uid for p in crud.list_projects(archive='active')] == [uid]


def test_archive_survives_manifest_rebuild_and_restores_to_same_directory(make_project):
    uid = make_project()
    root = crud.project_dir(uid)
    archived_at = set_archived(uid, True).archived_at
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['archived_at'] == archived_at.replace(tzinfo=datetime.timezone.utc).isoformat()
    _remove_index(uid)
    assert crud.rebuild_from_dirs()['projects'] == 1
    assert crud.get_project(uid).archived_at == archived_at
    assert crud.list_projects(archive='active') == []
    assert crud.project_dir(uid) == root
    assert crud.rebuild_from_dirs()['projects'] == 0
    set_archived(uid, False)
    assert [p.uid for p in crud.list_projects(archive='active')] == [uid]


@pytest.mark.parametrize('value', [None, '', '错误日期', '0001-01-01T00:00:00+23:59', 123, True, {}])
def test_legacy_or_invalid_manifest_archive_metadata_does_not_block_rebuild(make_project, value):
    uid = make_project()
    manifest = crud.project_dir(uid) / 'manifest.json'
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if value is None:
        data.pop('archived_at', None)
    else:
        data['archived_at'] = value
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    _remove_index(uid)
    assert crud.rebuild_from_dirs()['projects'] == 1
    assert crud.get_project(uid).archived_at is None


def test_manifest_archive_time_with_offset_is_normalized_to_utc(make_project):
    uid = make_project()
    manifest = crud.project_dir(uid) / 'manifest.json'
    data = json.loads(manifest.read_text(encoding='utf-8'))
    data['archived_at'] = '2026-10-07T20:00:00+08:00'
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    _remove_index(uid)
    assert crud.rebuild_from_dirs()['projects'] == 1
    assert crud.get_project(uid).archived_at.replace(tzinfo=datetime.timezone.utc) == datetime.datetime(
        2026, 10, 7, 12, tzinfo=datetime.timezone.utc)


def test_stale_save_snapshot_cannot_cancel_archive_or_reapply_restored_archive(make_project):
    uid = make_project()
    before_archive = crud.get_project(uid)
    timestamp = set_archived(uid, True).archived_at
    before_archive.title = '保存旧快照中的正常字段'
    crud.save_project(before_archive)
    assert crud.get_project(uid).archived_at == timestamp
    assert crud.get_project(uid).title == before_archive.title
    before_restore = crud.get_project(uid)
    set_archived(uid, False)
    before_restore.params = '{"正常参数":true}'
    crud.save_project(before_restore)
    assert crud.get_project(uid).archived_at is None
    assert crud.project_params(crud.get_project(uid)) == {'正常参数': True}


def test_archive_committed_between_save_read_and_write_is_preserved(make_project):
    """在旧保存真正 UPDATE 前插入归档，验证 SQL 本身排除管理字段。"""
    from sqlalchemy import event
    uid = make_project()
    snapshot = crud.get_project(uid)
    snapshot.title = '旧快照正常保存'
    inserted = False

    def archive_before_update(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal inserted
        if not inserted and statement.startswith('UPDATE project SET') and 'archived_at=' not in statement:
            inserted = True
            set_archived(uid, True)

    engine = db.get_engine()
    event.listen(engine, 'before_cursor_execute', archive_before_update)
    try:
        crud.save_project(snapshot)
    finally:
        event.remove(engine, 'before_cursor_execute', archive_before_update)
    assert inserted
    assert crud.get_project(uid).archived_at is not None
    assert snapshot.archived_at == crud.get_project(uid).archived_at
    assert crud.get_project(uid).title == snapshot.title


def test_archived_project_keeps_chain_when_old_page_requests_stop(make_project):
    uid = make_project(params={'_chain': ['split', 'translate'], '_chain_state': 'held',
        '_chain_hold': {'step': 'split', 'code': 'STEP_FAILED', 'message': '保留失败原因'}})
    set_archived(uid, True)
    before = crud.get_project(uid).model_dump()
    manifest = (crud.project_dir(uid) / 'manifest.json').read_bytes()
    response = TestClient(app).post(f'/api/projects/{uid}/chain/stop')
    assert response.status_code == 400
    assert response.json()['code'] == 'PROJECT_ARCHIVED'
    assert crud.get_project(uid).model_dump() == before
    assert (crud.project_dir(uid) / 'manifest.json').read_bytes() == manifest


@pytest.mark.parametrize('status', ['queued', 'running', 'stopping', 'cancelling'])
def test_archive_is_rejected_when_step_is_busy(make_project, status):
    uid = make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'download')
    step.status = status
    crud.save_step(step)
    with pytest.raises(runtime_edit.RuntimeEditError):
        set_archived(uid, True)
    assert crud.get_project(uid).archived_at is None


@pytest.mark.parametrize('registry', ['_current_jobs', '_ctxs', '_abandoned', '_retries'])
def test_archive_waits_for_background_cleanup_and_pending_retries(make_project, monkeypatch, registry):
    uid = make_project()
    project = crud.get_project(uid)
    marker = (project.id, 'download')
    monkeypatch.setattr(runner, registry, {marker} if registry == '_abandoned' else {marker: object()})
    with pytest.raises(runtime_edit.RuntimeEditError):
        set_archived(uid, True)
    assert crud.get_project(uid).archived_at is None
    # 共享夹具会读取重试项进行清理，测试恢复正常注册表。
    getattr(runner, registry).clear()


@pytest.mark.parametrize('kind', ['ingest', 'comments', 'dubbing', 'workflow', 'project_copy'])
def test_archive_rejects_non_pipeline_tasks(make_project, monkeypatch, kind):
    uid = make_project()
    project = crud.get_project(uid)
    if kind == 'dubbing':
        with dubbing_session.use(project, 1):
            with pytest.raises(runtime_edit.RuntimeEditError):
                set_archived(uid, True)
    elif kind in {'ingest', 'comments'}:
        with video_info.operation(uid, kind):
            with pytest.raises(runtime_edit.RuntimeEditError):
                set_archived(uid, True)
    else:
        if kind == 'workflow':
            monkeypatch.setattr(workflows, '_leases', {uid: '执行中的流程'})
        else:
            monkeypatch.setattr(project_copy, '_suggesting', {uid})
        with pytest.raises(runtime_edit.RuntimeEditError):
            set_archived(uid, True)
    assert crud.get_project(uid).archived_at is None


@pytest.mark.parametrize('status', ['queued', 'preparing', 'uploading', 'submitting', 'joining', 'stopping'])
def test_archive_rejects_publication_execution(make_project, status):
    uid = make_project()
    _publication(uid, status)
    with pytest.raises(runtime_edit.RuntimeEditError):
        set_archived(uid, True)
    assert crud.get_project(uid).archived_at is None


@pytest.mark.parametrize('kind', ['upload_finalizing', 'publication_copy', 'studio'])
def test_archive_rejects_publication_cleanup_copy_and_change_submission(make_project, monkeypatch, kind):
    uid = make_project()
    key = _publication(uid, 'paused' if kind == 'upload_finalizing' else 'draft')
    if kind == 'upload_finalizing':
        monkeypatch.setattr(service, '_running', {key: threading.Event()})
    elif kind == 'publication_copy':
        monkeypatch.setattr(service, '_suggesting', {key})
    else:
        with Session(db.get_engine()) as session:
            session.add(PublishChange(account_id='test', bvid='BV1234567890', kind='edit',
                publication_id=key, status='committing'))
            session.commit()
    with pytest.raises(runtime_edit.RuntimeEditError):
        set_archived(uid, True)
    assert crud.get_project(uid).archived_at is None


def test_remote_review_and_receipt_check_allow_archive_and_restore(make_project, monkeypatch):
    uid = make_project()
    key = _publication(uid, 'checking')
    with Session(db.get_engine()) as session:
        workflow = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='monitoring')
        session.add(workflow)
        session.commit()
        session.refresh(workflow)
        workflow_id = workflow.id
    monkeypatch.setattr(workflows, '_running', {workflow_id: threading.Event()})
    monkeypatch.setattr(service, '_running', {key: threading.Event()})
    assert set_archived(uid, True).archived_at is not None
    assert set_archived(uid, False).archived_at is None


def test_manifest_write_failure_can_be_repaired_by_idempotent_retry(make_project, monkeypatch):
    uid = make_project()
    write_manifest = crud.write_manifest
    monkeypatch.setattr(crud, 'write_manifest', lambda _uid: (_ for _ in ()).throw(OSError('磁盘暂不可写')))
    with pytest.raises(ProjectArchiveError) as error:
        set_archived(uid, True)
    assert error.value.code == 'PROJECT_ARCHIVE_WRITE_FAILED'
    archived = crud.get_project(uid)
    assert archived.archived_at is not None
    monkeypatch.setattr(crud, 'write_manifest', write_manifest)
    repaired = set_archived(uid, True)
    assert repaired.archived_at == archived.archived_at
    assert repaired.updated_at == archived.updated_at
    manifest = json.loads((crud.project_dir(uid) / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['archived_at'] == archived.archived_at.replace(tzinfo=datetime.timezone.utc).isoformat()
