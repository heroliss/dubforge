"""运行时编辑门禁：真实范围、旧入口、后台收尾和启动竞态；数据全部隔离。"""
import threading
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.main import app
from dubforge import configuration, config, runtime_edit, video_info, workflows
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.pipeline import dubbing_session
from dubforge.publishing import service, studio, project_copy
from dubforge.publishing.models import Publication, PublishChange
from dubforge.runner import runner
from dubforge.workflow_models import WorkflowRun


def _save(key, updates=None, **extra):
    current = configuration.resolve(key=key)
    return configuration.save(key, dict(revision=current['revision'],
        fingerprint=current['fingerprint'], updates=updates or {'assemble.crf': 19}), **extra)


def _step(uid, status, step_key='download'):
    project = crud.get_project(uid)
    step = crud.get_step(project.id, step_key)
    step.status = status
    crud.save_step(step)
    return project


def _publication(uid, status):
    row = Publication(project_uid=uid, status=status)
    with Session(get_engine()) as session:
        session.add(row); session.commit(); session.refresh(row)
        return row.id


@pytest.mark.parametrize('status', ['queued', 'running', 'stopping'])
def test_step_locks_settings_and_checkboxes_server_side(make_project, status):
    uid = make_project(); _step(uid, status)
    client = TestClient(app)
    response = client.get(f'/api/configuration/project:{uid}/editability')
    assert response.status_code == 200
    report = response.json()
    # ADR-0132: a running step freezes only the settings it consumes.  The
    # project/resource gate is reported separately for management operations.
    assert report['locked'] is False
    if status == 'queued':
        assert report['running_steps'] == []
        assert report['resource_locked'] and uid in report['resource_project_uids']
    else:
        assert any(row['step_key'] == 'download' for row in report['running_steps'])
        assert 'download.resolution' in report['locked_paths']
        assert report['resource_locked'] and uid in report['resource_project_uids']
    before = configuration.resolve(uid)
    saved = client.put(f'/api/configuration/project:{uid}', json={
        'revision': before['revision'], 'updates': {'assemble.crf': 19}})
    assert saved.status_code == 200
    current = configuration.resolve(uid)
    blocked = client.put(f'/api/configuration/project:{uid}', json={
        'revision': current['revision'], 'updates': {'download.resolution': 480}})
    if status == 'queued':
        assert blocked.status_code == 200
    else:
        assert blocked.json()['code'] == 'CONFIGURATION_BUSY'
    checked = client.put(f'/api/projects/{uid}/auto_steps', json={'steps': ['split']})
    # The selected chain is a future execution plan and remains editable while
    # a step is running/queued; starting another task is still gated.
    assert checked.status_code == 200
    renamed = client.patch(f'/api/projects/{uid}', json={'title': '不应保存'})
    assert renamed.json()['code'] == 'CONFIGURATION_BUSY'
    if status != 'queued':
        assert configuration.resolve(uid)['values']['download.resolution'] == current['values']['download.resolution']
    assert crud.project_params(crud.get_project(uid))['auto_steps'] == ['split']
    assert crud.get_project(uid).title != '不应保存'


def test_only_bound_projects_lock_a_series_and_global_covers_all(make_project):
    busy, idle = make_project(), make_project()
    template = configuration.create_template('活动系列')
    other = configuration.create_template('另一系列')
    plan = configuration.binding_plan([busy], template['key'][7:])
    configuration.bind(dict(uids=[busy], template_id=template['key'][7:],
        fingerprint=plan['fingerprint'], confirmed=True))
    _step(busy, 'running')
    series_report = runtime_edit.editability(template['key'])
    global_report = runtime_edit.editability('global')
    # ADR-0132 returns field-level locks for a running step instead of
    # disabling the whole series/global settings scope.
    assert not series_report['locked'] and busy in series_report['locked_paths_by_project']
    assert not global_report['locked'] and busy in global_report['locked_paths_by_project']
    assert runtime_edit.editability(other['key']) == dict(
        locked=False, reason='', project_uids=[], resource_locked=False,
        resource_reason='', resource_project_uids=[], running_steps=[],
        locked_paths=[], locked_paths_by_project={})
    _save('project:' + idle)
    _save(other['key'])
    with pytest.raises(runtime_edit.RuntimeEditError):
        configuration.archive(template['key'], {'revision': configuration.resolve(key=template['key'])['revision'], 'archived': True})
    assert configuration.resolve(key=template['key'])['archived'] is False


@pytest.mark.parametrize('registry', ['_current_jobs', '_ctxs', '_abandoned', '_retries'])
def test_finalized_step_state_does_not_unlock_a_live_worker(make_project, monkeypatch, registry):
    uid = make_project(); project = _step(uid, 'failed'); key = (project.id, 'download')
    marker = {key} if registry == '_abandoned' else {key: object()}
    monkeypatch.setattr(runner, registry, marker)
    report = runtime_edit.editability('project:' + uid)
    assert report['locked'] is False
    assert report['resource_locked']
    # A stale worker/retry marker is still a project resource lock, while
    # unrelated settings remain editable under the step-scoped policy.
    _save('project:' + uid)
    marker.clear()
    assert not runtime_edit.editability('project:' + uid)['locked']


def test_single_line_dubbing_and_video_operations_lock_their_project(make_project, monkeypatch):
    uid = make_project(); project = crud.get_project(uid)
    with dubbing_session.use(project, 1):
        report = runtime_edit.editability('project:' + uid)
        assert not report['locked']
        assert any(row['step_key'] == 'tts' for row in report['running_steps'])
        with pytest.raises(runtime_edit.RuntimeEditError):
            _save('global', {'tts.backend': 'gptsovits'})
    with video_info.operation(uid, 'test_comments'):
        report = runtime_edit.editability('project:' + uid)
        assert not report['locked']
        assert any(row['step_key'] == 'download' for row in report['running_steps'])
        with pytest.raises(runtime_edit.RuntimeEditError):
            with dubbing_session.edit(project):
                pytest.fail('活动资料任务不能绕过配音编辑入口')
    assert not runtime_edit.editability('project:' + uid)['locked']


@pytest.mark.parametrize('status', ['queued', 'running', 'stopping'])
def test_workflow_between_steps_remains_locked(make_project, status):
    uid = make_project()
    with Session(get_engine()) as session:
        session.add(WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status=status))
        session.commit()
    report = runtime_edit.editability('project:' + uid)
    assert not report['locked']
    assert report['resource_locked'] and uid in report['resource_project_uids']
    with pytest.raises(runtime_edit.RuntimeEditError):
        with runtime_edit.editing('project:' + uid):
            pass


def test_malformed_running_workflow_plan_keeps_resource_gate_available(make_project):
    uid = make_project()
    with Session(get_engine()) as session:
        session.add(WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()),
                                status='running', plan='[]'))
        session.commit()
    report = runtime_edit.editability('project:' + uid)
    assert not report['locked']
    assert report['resource_locked'] and uid in report['resource_project_uids']


def test_remote_monitoring_and_read_only_receipt_query_do_not_lock(make_project, monkeypatch):
    uid = make_project()
    with Session(get_engine()) as session:
        row = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='monitoring')
        session.add(row); session.commit(); session.refresh(row); key = row.id
    publication = _publication(uid, 'checking')
    monkeypatch.setattr(workflows, '_running', {key: threading.Event()})
    monkeypatch.setattr(service, '_running', {publication: threading.Event()})
    assert runtime_edit.editability('project:' + uid) == dict(
        locked=False, reason='', project_uids=[], resource_locked=False,
        resource_reason='', resource_project_uids=[], running_steps=[],
        locked_paths=[], locked_paths_by_project={})
    _save('project:' + uid)


@pytest.mark.parametrize('status', ['queued', 'preparing', 'uploading', 'submitting', 'joining', 'stopping'])
def test_publication_local_execution_locks_project_edits(make_project, status):
    uid = make_project(); _publication(uid, status)
    report = runtime_edit.editability('project:' + uid)
    assert not report['locked']
    assert report['resource_locked'] and uid in report['resource_project_uids']
    with pytest.raises(runtime_edit.RuntimeEditError):
        with dubbing_session.edit(crud.get_project(uid)):
            pytest.fail('发布活动不能绕过字幕/配音编辑入口')


def test_publication_failed_state_waits_for_worker_cleanup(make_project, monkeypatch):
    uid = make_project(); key = _publication(uid, 'paused')
    monkeypatch.setattr(service, '_running', {key: threading.Event()})
    report = runtime_edit.editability('project:' + uid)
    assert not report['locked'] and report['resource_locked']
    service._running.clear()
    assert not runtime_edit.editability('project:' + uid)['locked']


@pytest.mark.parametrize('start', ['step', 'dubbing'])
def test_new_tasks_are_rejected_during_publication_preparation(make_project, monkeypatch, start):
    uid = make_project(); key = _publication(uid, 'draft')
    monkeypatch.setattr(service, '_running', {key: threading.Event()})
    with pytest.raises(runtime_edit.RuntimeEditError):
        if start == 'step':
            runner.start_step(uid, 'download')
        else:
            runner.start_dubbing(uid, [1])


@pytest.mark.parametrize('kind', ['project_copy', 'publication_copy', 'studio'])
def test_copy_generation_and_local_publication_change_claims_are_locked(make_project, monkeypatch, kind):
    uid = make_project(); key = _publication(uid, 'draft')
    if kind == 'project_copy':
        monkeypatch.setattr(project_copy, '_suggesting', {uid})
    elif kind == 'publication_copy':
        monkeypatch.setattr(service, '_suggesting', {key})
    else:
        with Session(get_engine()) as session:
            session.add(PublishChange(account_id='test', bvid='BV1234567890', kind='edit',
                publication_id=key, status='committing'))
            session.commit()
    report = runtime_edit.editability('project:' + uid)
    assert not report['locked']
    assert report['resource_locked'] and uid in report['resource_project_uids']
    with pytest.raises(runtime_edit.RuntimeEditError):
        with runtime_edit.editing('project:' + uid):
            pass


def test_display_and_next_boundary_timeout_can_save_during_running_step(make_project):
    uid = make_project(); config.update_config({'llm.timeout': 1800}); _step(uid, 'running', step_key='split')
    client = TestClient(app)
    result = client.post('/api/settings', json={'updates': {'ui.llm_stream.reference_distance_chars': 800}})
    assert result.status_code == 200
    assert config.get('ui.llm_stream.reference_distance_chars') == 800
    blocked = client.post('/api/settings', json={'updates': {
        'ui.llm_stream.reference_distance_chars': 900, 'llm.timeout': 60}})
    # 请求时限只在下一次 LLM 请求边界读取；当前步骤已冻结旧值，
    # 因而可以与显示偏好一起保存，不会改变本次正在运行的请求。
    assert blocked.status_code == 200
    assert config.get('ui.llm_stream.reference_distance_chars') == 900
    assert config.get('llm.timeout') == 60
    # A nested display-only update is a regular settings request and remains
    # independent from the locked LLM execution path.
    assert client.post('/api/settings', json={'updates': {'ui': {'llm_stream': {'reference_distance_chars': 900}}}}).status_code == 200


def test_tts_timeout_and_service_endpoint_can_save_for_next_boundary(make_project):
    uid = make_project()
    _step(uid, 'running', step_key='tts')
    client = TestClient(app)
    # The TTS worker has already captured its snapshot.  Changing its guard
    # and endpoint is safe for the next invocation, while a content parameter
    # consumed by the active step remains protected.
    response = client.put(f'/api/configuration/project:{uid}', json={
        'revision': configuration.resolve(uid)['revision'],
        'updates': {
            # 43200s（12 小时）是长视频配音可用的项目级兜底；该调整
            # 发生在正在运行的步骤期间，应只影响下一次步骤边界。
            'runner.step_timeout_seconds.tts': 43200,
            'tts.indextts25.request_timeout_seconds': 900,
            'tts.indextts25.url': 'http://127.0.0.1:7860/',
        },
    })
    assert response.status_code == 200
    current = configuration.resolve(uid)
    assert current['values']['runner.step_timeout_seconds.tts'] == 43200
    assert current['values']['tts.indextts25.request_timeout_seconds'] == 900
    assert current['values']['tts.indextts25.url'] == 'http://127.0.0.1:7860/'
    blocked = client.put(f'/api/configuration/project:{uid}', json={
        'revision': current['revision'],
        'updates': {'tts.backend': 'gptsovits'},
    })
    assert blocked.json()['code'] == 'CONFIGURATION_BUSY'


def test_internal_workflow_ownership_only_authorizes_its_own_project(make_project, monkeypatch):
    uid = make_project(); other = make_project()
    monkeypatch.setattr(workflows, '_leases', {uid: 'actual-run', other: 'other-run'})
    with workflows._owned('actual-run'):
        report = runtime_edit.editability('project:' + uid)
        assert not report['locked'] and report['resource_locked']
        _save('project:' + uid)
        # Settings are scoped by the active execution step; the workflow
        # resource gate still blocks project-level operations.
        _save('project:' + other)
        _save('global')
        with pytest.raises(runtime_edit.RuntimeEditError):
            with runtime_edit.editing('project:' + uid):
                pytest.fail('外部编辑不能仅凭同线程所有者绕过')
        with pytest.raises(runtime_edit.RuntimeEditError):
            with runtime_edit.editing('project:' + other):
                pytest.fail('工作流占用期间不能执行项目级操作')


def test_step_internal_write_requires_the_actual_registered_context(make_project, monkeypatch):
    uid = make_project(); project = _step(uid, 'running'); key = (project.id, 'assemble')
    context = SimpleNamespace(project=project, step_key='assemble')
    monkeypatch.setattr(runner, '_ctxs', {key: context})
    monkeypatch.setattr(runner, '_current_jobs', {key: object()})
    with runtime_edit.pipeline_writes(context):
        _save('project:' + uid)
    forged = SimpleNamespace(project=project, step_key='assemble')
    with runtime_edit.pipeline_writes(forged), pytest.raises(runtime_edit.RuntimeEditError):
        _save('project:' + uid)


def test_pure_comment_plan_save_cannot_bypass_configuration_gate(make_project, monkeypatch):
    from dubforge import comment_studio
    uid = make_project(); _step(uid, 'running', step_key='assemble')
    monkeypatch.setattr(comment_studio, '_write', lambda *a: pytest.fail('不能写入活动项目的评论方案'))
    state = comment_studio.view(uid)
    with pytest.raises(runtime_edit.RuntimeEditError):
        comment_studio.save(uid, {'revision': state['overlay_revision'], 'items': [], 'settings': state['defaults']})


def test_video_metadata_actions_cannot_start_during_an_unrelated_step(make_project, monkeypatch):
    uid = make_project(); _step(uid, 'running')
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: pytest.fail('被锁定时不能发网络请求'))
    with pytest.raises(runtime_edit.RuntimeEditError):
        video_info.update(uid, 'comments')


def test_config_commit_holds_start_gate_until_the_new_values_are_saved(make_project, monkeypatch):
    uid = make_project(); entered = threading.Event(); release = threading.Event()
    attempting = threading.Event(); started = threading.Event(); errors = []

    def before_commit():
        entered.set()
        assert release.wait(3)

    def start(uid, key, force=False, **options):
        assert options.get('dubbing') is None
        assert configuration.resolve(uid)['values']['assemble.crf'] == 19
        started.set()
        return {'status': 'started'}

    def run_save():
        try: _save('project:' + uid, before_commit=before_commit)
        except BaseException as exc: errors.append(exc)

    def run_start():
        attempting.set()
        try: runner.start_step(uid, 'download')
        except BaseException as exc: errors.append(exc)

    monkeypatch.setattr(runner, '_start_step', start)
    saver = threading.Thread(target=run_save); starter = threading.Thread(target=run_start)
    saver.start()
    try:
        assert entered.wait(3)
        starter.start(); assert attempting.wait(3)
        assert not started.wait(.1)
    finally:
        release.set(); saver.join(3)
        if starter.ident is not None: starter.join(3)
    assert not saver.is_alive() and not starter.is_alive()
    assert not errors and started.is_set()


def test_start_claim_is_seen_by_a_save_waiting_on_the_same_gate(make_project, monkeypatch):
    uid = make_project(); entered = threading.Event(); release = threading.Event()
    attempting = threading.Event(); finished = threading.Event(); errors = []
    before = configuration.resolve(uid)

    def start(uid, key, force=False, **options):
        assert options.get('dubbing') is None
        _step(uid, 'queued'); entered.set(); assert release.wait(3)
        return {'status': 'started'}

    def run_start():
        try: runner.start_step(uid, 'download')
        except BaseException as exc: errors.append(exc)

    def run_save():
        attempting.set()
        try: _save('project:' + uid)
        except BaseException as exc: errors.append(exc)
        finally: finished.set()

    monkeypatch.setattr(runner, '_start_step', start)
    starter = threading.Thread(target=run_start); saver = threading.Thread(target=run_save)
    starter.start()
    try:
        assert entered.wait(3)
        saver.start(); assert attempting.wait(3)
        assert not finished.wait(.1)
    finally:
        release.set(); starter.join(3)
        if saver.ident is not None: saver.join(3)
    assert not starter.is_alive() and not saver.is_alive()
    # Queueing is the edit boundary in ADR-0132: unrelated settings may be
    # changed before the worker obtains its slot and captures its snapshot.
    assert not errors
    assert configuration.resolve(uid)['revision'] == before['revision'] + 1
