"""冷首屏不读取媒体、逐句资料或完整执行配方；真实核验仍由 full/plan 承担。"""
import datetime
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from sqlalchemy import event

from dubforge.domain import crud
from dubforge.domain.models import Job
from dubforge.errors import DubForgeError
from dubforge.project_list_summary import project_list
from dubforge.publishing.models import Publication, PublicationUpdate, PublishAccount
from dubforge.workflow_models import WorkflowRun


def _row(uid, *, archive='active'):
    return next(row for row in project_list(archive=archive) if row['uid'] == uid)


def _save(*rows):
    with crud.session() as session:
        for row in rows:
            session.add(row)
        session.commit()
        for row in rows:
            session.refresh(row)


def _client():
    # 不进入生产 app/lifespan；路由合同无需启动后台监视或真实服务。
    from app.api.projects import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_summary_cold_list_forbids_heavy_paths_and_keeps_unknown_freshness(make_project, monkeypatch):
    from dubforge import comment_studio, configuration
    # 先定义恢复依赖子类，再把父类构造器换成禁止重路径的探针；单文件也可独立运行。
    from dubforge.pipeline import dependencies, recovery, stale_explanation
    from dubforge.publishing import display, service
    uid = make_project(params={'auto_steps': ['split', 'translate'], '_chain': ['split'],
        '_chain_state': 'held', '_chain_hold': {'step': 'split', 'code': 'LLM_RETRY', 'message': '待处理',
                                             'private': '不返回'},
        'tts': {'private_legacy': '隔离大型逐句参数' * 100000}})
    project = crud.get_project(uid)
    project.status = 'done'
    step = crud.get_step(project.id, 'split')
    step.status, step.progress = 'done', 100
    _save(project, step)
    before = project.model_dump()

    def forbidden(*args, **kwargs):
        pytest.fail('摘要禁止执行产物/文件/配置解析/资料扫描')
    monkeypatch.setattr(dependencies, 'Dependencies', forbidden)
    monkeypatch.setattr(dependencies, 'project_status', forbidden)
    monkeypatch.setattr(stale_explanation, 'capture', forbidden)
    monkeypatch.setattr(crud, 'project_params', forbidden)
    monkeypatch.setattr(crud, 'project_dir_by_project', forbidden)
    monkeypatch.setattr(crud, 'latest_artifacts', forbidden)
    monkeypatch.setattr(configuration, 'resolve', forbidden)
    monkeypatch.setattr(service, 'project_statuses', forbidden)
    monkeypatch.setattr(comment_studio, 'workspace_summaries', forbidden)
    monkeypatch.setattr(display, 'resolve', forbidden)
    response = _client().get('/api/projects?view=summary')
    assert response.status_code == 200
    assert response.headers['x-dubforge-projection'] == 'summary'
    assert response.headers['x-dubforge-workspace'] and response.headers['cache-control'] == 'no-store'
    assert len(response.content) < 10000
    row = response.json()[0]
    assert row['uid'] == uid and row['status'] == 'checking' and row['recorded_status'] == 'done'
    assert row['freshness_known'] is False and row['dir_exists'] is None
    assert row['projection'] == 'summary' and len(row['summary_revision']) == 64
    assert row['chain'] == dict(remaining=['split'], state='held',
        hold={'step': 'split', 'code': 'LLM_RETRY', 'message': '待处理'})
    assert 'tts' not in row['params'] and row['comments_workspace'] is None
    assert 'private' not in json.dumps(row['params'])
    assert {step['key'] for step in row['steps'] if step['selected']} == {'split', 'translate'}
    assert all(step['fresh'] is None and not step['freshness_known'] for step in row['steps'])
    assert next(step for step in row['steps'] if step['key'] == 'split')['status'] == 'done'
    assert crud.get_project(uid).model_dump() == before


@pytest.mark.parametrize('recorded', ['created', 'done', 'failed', 'processing', 'unknown'])
def test_inactive_recorded_status_never_proves_current_completion(make_project, recorded):
    uid = make_project()
    project = crud.get_project(uid)
    project.status = recorded
    _save(project)
    row = _row(uid)
    assert row['status'] == 'checking' and row['recorded_status'] == recorded
    assert row['running_step'] is None and row['workflow_status'] is None


def test_new_job_overrides_old_failed_step_without_old_progress_or_error(make_project):
    uid = make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'tts')
    step.status, step.progress, step.error_message = 'failed', 83, '上次失败'
    job = Job(project_id=project.id, step_key='tts', status='running',
        execution=json.dumps(dict(schema=1, label='处理第 454 句配音', phase='tts.synthesizing',
                                   completed=453, total=646, unit='句')))
    _save(step, job)
    row = _row(uid)
    assert row['status'] == 'processing' and row['running_step'] == 'tts'
    tts = next(step for step in row['steps'] if step['key'] == 'tts')
    assert tts['status'] == 'running' and tts['recorded_status'] == 'failed'
    assert tts['error'] is None and tts['progress'] == 0
    assert tts['job_id'] == job.id and tts['execution']['completed'] == 453
    assert tts['execution']['total'] == 646
    # Job.running 包含资源排队，真实 StepRun.queued 必须保留等待语义。
    step.status = 'queued'
    _save(step)
    assert next(step for step in _row(uid)['steps'] if step['key'] == 'tts')['status'] == 'queued'


def test_real_step_failure_is_kept_without_terminal_execution_history(make_project):
    uid = make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'split')
    step.status, step.error_message = 'failed', '明确的配置错误'
    _save(step, Job(project_id=project.id, step_key='split', status='failed',
        execution=json.dumps(dict(phase='split.llm', label='上次错误', history=[{'label': '旧过程'}]))))
    row = _row(uid)
    split = next(step for step in row['steps'] if step['key'] == 'split')
    assert row['status'] == 'checking'
    assert split['status'] == 'failed' and split['error'] == '明确的配置错误'
    assert split['execution'] is None and split['fresh'] is None


@pytest.mark.parametrize('job_status', ['cancelled', 'dead', 'failed', 'done', 'queued', 'running'])
def test_summary_latest_job_status_is_bound_to_the_same_job_id_without_claiming_freshness(make_project, job_status):
    uid = make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'assemble')
    step.status, step.error_message = 'failed', '旧步骤状态不能解释正常停止'
    old = Job(project_id=project.id, step_key='assemble', status='failed')
    _save(step, old)
    latest = Job(project_id=project.id, step_key='assemble', status=job_status)
    _save(latest)
    row = _row(uid)
    assemble = next(value for value in row['steps'] if value['key'] == 'assemble')
    assert assemble['job_id'] == latest.id and assemble['job_id'] != old.id
    assert assemble['job_status'] == job_status and assemble['recorded_status'] == 'failed'
    assert assemble['fresh'] is None and assemble['freshness_known'] is False
    assert row['status'] == ('processing' if job_status in {'queued', 'running'} else 'checking')
    assert row['dir_exists'] is None and row['freshness_known'] is False
    untouched = next(value for value in row['steps'] if value['key'] == 'download')
    assert untouched['job_id'] is None and untouched['job_status'] is None
    if job_status not in {'queued', 'running'}:
        assert assemble['execution'] is None


@pytest.mark.parametrize('job_status,expected', [('done', 'done'), ('failed', 'failed'),
    ('cancelled', 'interrupted'), ('dead', 'interrupted')])
def test_newer_terminal_job_cannot_reanimate_earlier_running_step(make_project, job_status, expected):
    uid = make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'tts')
    step.status = 'running'
    _save(step, Job(project_id=project.id, step_key='tts', status=job_status))
    row = _row(uid)
    tts = next(step for step in row['steps'] if step['key'] == 'tts')
    assert row['status'] == 'checking' and row['running_step'] is None
    assert tts['status'] == expected and tts['execution'] is None


@pytest.mark.parametrize('status,phase,comments', [
    ('running', '评论评分：本次500条，复用有效评分', True),
    ('running', '评论翻译：已选12/30条', True),
    ('paused', '评论评分：本次500条', False),
    ('failed', '评论评分：本次500条', False),
    ('unknown', '评论评分：本次500条', False),
    ('running', '已投稿，等待平台审核', False),
])
def test_workflow_summary_uses_known_activity_without_reading_frozen_plan(make_project, status, phase, comments):
    uid = make_project()
    workflow = WorkflowRun(request_id=uid, project_uid=uid, status=status, phase=phase, cursor=6,
        plan='损坏且不能读取的大配方', recipe='{"mode":"publish"}')
    _save(workflow)
    row = _row(uid)
    assert row['workflow_summary'] == dict(id=workflow.id, status=status, phase=phase, cursor=6, publication_id='')
    assert row['workflow_status'] == status
    assert bool(row['comments_workspace']) is comments
    if comments:
        assert row['status'] == 'processing'
        assert row['comments_workspace']['state'] == 'running'
        assert 'count' not in row['comments_workspace']


def test_memory_comment_worker_and_manual_dubbing_are_positive_activity(make_project, monkeypatch):
    from dubforge import video_info
    from dubforge.pipeline import dubbing_session
    uid = make_project()
    monkeypatch.setitem(video_info._active, uid, 'prepare_comments')
    assert _row(uid)['comments_workspace']['state'] == 'running'
    monkeypatch.delitem(video_info._active, uid)
    monkeypatch.setitem(dubbing_session._active, uid, {'active_line': 17})
    row = _row(uid)
    assert row['status'] == 'processing' and row['running_step'] is None
    assert row['comments_workspace'] is None
    assert all(step['execution'] is None for step in row['steps'])


def test_archive_filter_and_damaged_minimal_params_are_bounded(make_project):
    uid, archived = make_project(), make_project()
    project, hidden = crud.get_project(uid), crud.get_project(archived)
    hidden.archived_at = datetime.datetime.now(datetime.timezone.utc)
    project.params = '{broken'
    _save(project, hidden)
    assert [row['uid'] for row in project_list()] == [uid]
    assert [row['uid'] for row in project_list(archive='archived')] == [archived]
    assert {row['uid'] for row in project_list(archive='all')} == {uid, archived}
    assert _row(uid)['params'] == {} and _row(uid)['status'] == 'checking'
    with pytest.raises(DubForgeError) as error:
        project_list(archive='invalid')
    assert error.value.code == 'PROJECT_ARCHIVE_FILTER_INVALID'


def test_publication_uses_exact_active_record_and_minimal_update_without_file_reads(make_project):
    uid = make_project()
    active = Publication(id='active', project_uid=uid, status='published', receipt='{"bvid":"BVExact"}',
        snapshot='冻结视频列表' * 100000, transfers='损坏的旧传输表')
    archived = Publication(id='archived', project_uid=uid, status='failed',
        archived_at=datetime.datetime.now(datetime.timezone.utc), receipt='{"bvid":"BVOld"}')
    update = PublicationUpdate(id='verified', project_uid=uid, source_publication_id=active.id, account_id='account',
        bvid='BVExact', status='completed', progress=100, plan='冻结更新方案' * 100000,
        execution=json.dumps({'stages': {'verify': {'status': 'completed', 'progress': 100}},
                              'transfers': {'不加载的逐文件证据': '隔离传输数据' * 100000}}))
    _save(active, archived, update)
    row = _row(uid)
    publication = row['publication']
    assert publication['id'] == 'active' and publication['bvid'] == 'BVExact'
    assert publication['status'] == 'published' and publication['execution'] is None
    assert publication['latest_update']['id'] == 'verified'
    assert publication['latest_update']['status'] == 'completed'
    assert len(json.dumps(publication)) < 5000
    assert 'plan' not in json.dumps(publication) and 'snapshot' not in json.dumps(publication)
    assert row['status'] == 'checking', '发布回执不能替代制作产物核验'


def test_settings_summary_skips_display_cache_and_revisions_revoke_full_token(make_project, monkeypatch):
    from dubforge.configuration_models import ConfigurationScope
    from dubforge.publishing import display
    uid = make_project()
    with crud.session() as session:
        scope = session.get(ConfigurationScope, 'project:' + uid)
        scope.values = json.dumps({'run.mode': 'publish', 'publish.account_id': 'account'})
        session.add(scope)
        session.add(PublishAccount(id='account', name='数据库账号名', credential='不应输出'))
        session.commit()
    monkeypatch.setattr(display, 'resolve', lambda *a, **kw: pytest.fail('不能读取缓存显示名'))
    first = _row(uid)
    assert first['project_settings']['account_name'] == '数据库账号名'
    assert first['project_settings']['season_title'] == ''
    assert '不应输出' not in json.dumps(first)
    with crud.session() as session:
        scope = session.get(ConfigurationScope, 'project:' + uid)
        scope.revision += 1
        session.add(scope); session.commit()
    assert _row(uid)['summary_revision'] != first['summary_revision']


def test_revision_is_stable_for_heartbeat_but_changes_for_new_job_and_terminal(make_project):
    uid = make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'tts')
    step.status = 'running'
    job = Job(project_id=project.id, step_key='tts', status='running', execution='{}')
    _save(step, job)
    first = _row(uid)['summary_revision']
    step.progress = 50
    job.execution = json.dumps({'completed': 323, 'total': 646, 'updated_at': '2026-10-20T00:00:00Z'})
    job.heartbeat_at = datetime.datetime.now(datetime.timezone.utc)
    _save(step, job)
    assert _row(uid)['summary_revision'] == first
    job2 = Job(project_id=project.id, step_key='tts', status='running')
    _save(job2)
    second = _row(uid)['summary_revision']
    assert second != first
    step.status, job2.status = 'done', 'done'
    _save(step, job2)
    assert _row(uid)['summary_revision'] != second


def test_workflow_phase_and_upload_progress_do_not_revoke_same_generation(make_project):
    uid = make_project()
    workflow = WorkflowRun(request_id=uid, project_uid=uid, status='running',
        phase='评论评分：本次500条', cursor=6, recipe='{"mode":"publish"}')
    publication = Publication(project_uid=uid, status='uploading')
    _save(workflow, publication)
    first = _row(uid)['summary_revision']
    workflow.phase = '评论评分：本次500条，已完成240条'
    publication.progress = 52
    publication.updated_at = datetime.datetime.now(datetime.timezone.utc)
    _save(workflow, publication)
    assert _row(uid)['summary_revision'] == first
    workflow.cursor += 1
    _save(workflow)
    assert _row(uid)['summary_revision'] != first


def test_summary_queries_are_batched_and_exclude_heavy_columns(make_project):
    from dubforge.db import get_engine
    make_project()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)
    engine = get_engine()
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        project_list()
        initial = list(statements)
        for _ in range(11):
            make_project()
        statements.clear()
        result = project_list()
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert len(result) == 12 and len(statements) == len(initial)
    assert len(statements) <= 11
    sql = '\n'.join(statements).lower()
    assert 'artifact.' not in sql and 'steprun.input_snapshot' not in sql
    assert 'job.task_data' not in sql and 'workflowrun.plan' not in sql
    assert 'publication.snapshot' not in sql and 'publicationupdate.plan' not in sql
    assert 'publishaccount.credential' not in sql
    project_query = next(statement for statement in statements if 'FROM project ' in statement)
    assert 'json_extract' in project_query and 'project.params,' not in project_query


def test_summary_and_full_have_separate_single_flights_and_no_completed_cache(isolated_env, monkeypatch):
    from app.api import projects
    entered, release = threading.Event(), threading.Event()
    calls = []
    def summary(archive):
        calls.append(('summary', archive)); entered.set()
        assert release.wait(3)
        return [{'uid': 'summary', 'projection': 'summary'}]
    def full(archive):
        calls.append(('full', archive)); return [{'uid': 'full'}]
    monkeypatch.setattr(projects, '_build_project_summary', summary)
    monkeypatch.setattr(projects, '_build_project_list', full)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(projects.list_projects, view='summary')
        try:
            assert entered.wait(3)
            response = Response()
            assert projects.list_projects(response=response) == [{'uid': 'full'}]
            assert 'x-dubforge-projection' not in response.headers
        finally:
            release.set()
        assert pending.result()[0]['uid'] == 'summary'
    assert calls == [('summary', 'active'), ('full', 'active')]
    projects.list_projects(view='summary')
    assert calls[-1] == ('summary', 'active') and len(calls) == 3
    assert not projects._list_flights


def test_summary_single_flight_failure_releases_and_fresh_bypasses(isolated_env, monkeypatch):
    from app.api import projects
    calls = []
    def failed(archive):
        calls.append(archive)
        raise DubForgeError('隔离数据库读取失败', code='ISOLATED_SUMMARY_FAILURE')
    monkeypatch.setattr(projects, '_build_project_summary', failed)
    for _ in range(2):
        with pytest.raises(DubForgeError) as error:
            projects.list_projects(view='summary')
        assert error.value.code == 'ISOLATED_SUMMARY_FAILURE'
    assert calls == ['active', 'active'] and not projects._list_flights
    request = Request({'type': 'http', 'headers': [(b'x-dubforge-refresh', b'fresh')]})
    monkeypatch.setattr(projects, '_build_project_summary', lambda archive: [{'uid': 'fresh-summary'}])
    assert projects.list_projects(request=request, view='summary') == [{'uid': 'fresh-summary'}]
    with pytest.raises(DubForgeError) as error:
        projects.list_projects(view='unknown')
    assert error.value.code == 'PROJECT_LIST_VIEW_INVALID'
