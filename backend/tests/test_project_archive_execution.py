"""归档项目的执行边界；真实配置/调度入口，网络与真实项目完全隔离。"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.main import app
from dubforge import configuration, runtime_edit, video_info, workflows
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.domain.models import Job
from dubforge.project_archive import ProjectArchiveError, set_archived
from dubforge.publishing import service
from dubforge.runner import runner
from dubforge.workflow_models import WorkflowRun


def test_archive_locks_project_and_keeps_fixed_defaults_until_restore(make_project):
    uid = make_project()
    set_archived(uid, True)
    client = TestClient(app)
    report = client.get(f'/api/configuration/project:{uid}/editability').json()
    assert report['locked'] and report['project_uids'] == [uid]
    assert '已归档' in report['reason']
    before = configuration.resolve(uid)
    response = client.put(f'/api/configuration/project:{uid}', json={
        'revision': before['revision'], 'updates': {'assemble.crf': 19}})
    assert response.json()['code'] == 'PROJECT_ARCHIVED'
    response = client.put(f'/api/projects/{uid}/auto_steps', json={'steps': ['split']})
    assert response.json()['code'] == 'PROJECT_ARCHIVED'
    response = client.patch(f'/api/projects/{uid}', json={'title': '不能写入'})
    assert response.json()['code'] == 'PROJECT_ARCHIVED'
    assert configuration.resolve(uid) == before
    assert not runtime_edit.editability('global')['locked']
    parent = configuration.resolve(key='global')
    configuration.save('global', dict(revision=parent['revision'],
        fingerprint=parent['fingerprint'], updates={'assemble.crf': 19}))
    assert configuration.resolve(uid)['values']['assemble.crf'] == before['values']['assemble.crf']
    assert configuration.resolve(make_project())['values']['assemble.crf'] == 19
    assert crud.get_project(uid).archived_at is not None
    set_archived(uid, False)
    assert not runtime_edit.editability('project:' + uid)['locked']


def test_archived_runner_rejects_before_cached_skip_or_queue(make_project):
    uid = make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'download')
    step.status = 'done'
    crud.save_step(step)
    set_archived(uid, True)
    for force in (False, True):
        with pytest.raises(ProjectArchiveError) as exc:
            runner.start_step(uid, 'download', force=force)
        assert exc.value.code == 'PROJECT_ARCHIVED'
    assert crud.get_step(project.id, 'download').status == 'done'
    with Session(get_engine()) as session:
        assert not session.exec(select(Job).where(Job.project_id == project.id)).all()


def test_archived_video_operation_never_registers_or_runs_io(make_project):
    uid = make_project()
    set_archived(uid, True)
    with pytest.raises(ProjectArchiveError):
        with video_info.operation(uid, 'refresh'):
            pytest.fail('归档项目不得进入资料刷新/评论生成')
    assert uid not in video_info._active


def test_archived_workflow_rejected_before_online_preflight(make_project, monkeypatch):
    uid = make_project()
    set_archived(uid, True)
    monkeypatch.setattr(workflows, '_plan', lambda *args, **kwargs: pytest.fail('归档项目不能请求在线服务'))
    with pytest.raises(ProjectArchiveError):
        workflows.start(uid, dict(confirmed=True, request_id=str(uuid.uuid4())))
    with Session(get_engine()) as session:
        assert not session.exec(select(WorkflowRun)).all()


def test_archive_between_workflow_preflight_and_launch_is_rechecked(make_project, monkeypatch):
    uid = make_project()
    def archive_during_preflight(*args, **kwargs):
        set_archived(uid, True)
        return dict(blockers=[], fingerprint='fixture', recipe={'mode': 'produce'})
    monkeypatch.setattr(workflows, '_plan', archive_during_preflight)
    with pytest.raises(ProjectArchiveError):
        workflows.start(uid, dict(confirmed=True, request_id=str(uuid.uuid4()), fingerprint='fixture'))
    with Session(get_engine()) as session:
        assert not session.exec(select(WorkflowRun)).all()
    assert uid not in workflows._leases


def _failed_flow(uid, *, publication_id=''):
    with Session(get_engine()) as session:
        row = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='failed',
            publication_id=publication_id, recipe=json.dumps({'mode': 'produce'}))
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def test_archived_production_resume_rejected_without_changing_run(make_project, monkeypatch):
    uid = make_project()
    key = _failed_flow(uid)
    set_archived(uid, True)
    monkeypatch.setattr(workflows, '_plan', lambda *args, **kwargs: pytest.fail('不得为归档恢复做在线预检'))
    with pytest.raises(ProjectArchiveError):
        workflows.resume(key, dict(confirmed=True))
    assert workflows.get(key)['status'] == 'failed'
    assert uid not in workflows._leases
    assert '项目已归档' in '；'.join(workflows._availability(uid, 'produce', key))


def test_archived_remote_audit_resume_remains_available(make_project, monkeypatch):
    uid = make_project()
    key = _failed_flow(uid, publication_id='fixture-pub')
    set_archived(uid, True)
    monkeypatch.setattr(service, 'view', lambda key: dict(id=key, status='submitted', progress=0,
        error='', receipt={'bvid': 'BV-fixture'}))
    run = workflows.resume(key, dict(confirmed=True))
    assert run['status'] == 'monitoring'
    assert crud.get_project(uid).archived_at is not None
    assert uid not in workflows._leases


def test_archived_project_cannot_create_new_publication(make_project):
    uid = make_project()
    set_archived(uid, True)
    with pytest.raises(ProjectArchiveError):
        service.create(uid)


def test_archived_output_cannot_be_recycled_from_a_stale_page(make_project):
    uid = make_project()
    set_archived(uid, True)
    with pytest.raises(ProjectArchiveError):
        with runner.output_file_edit(uid):
            pytest.fail('归档项目产物不能从旧页面回收或清除登记')


def test_archived_review_report_stays_viewable_but_cannot_be_overwritten(make_project):
    uid = make_project()
    root = crud.project_dir(uid)
    report_file = root / 'content_review.json'
    report_file.write_text('{"fixture": "保留原有报告"}', encoding='utf-8')
    before = report_file.read_bytes()
    set_archived(uid, True)
    client = TestClient(app)
    assert client.get(f'/api/projects/{uid}/content-review').status_code == 200
    response = client.post(f'/api/projects/{uid}/content-review')
    assert response.json()['code'] == 'PROJECT_ARCHIVED'
    assert report_file.read_bytes() == before


def test_archive_busy_response_explains_the_requested_action(make_project):
    uid = make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'download')
    step.status = 'running'
    crud.save_step(step)
    response = TestClient(app).put(f'/api/projects/{uid}/archive', json={'archived': True})
    assert response.json()['code'] == 'CONFIGURATION_BUSY'
    assert '暂不能归档' in response.json()['message']
    assert '上传' in response.json()['suggestion']
    assert crud.get_project(uid).archived_at is None
