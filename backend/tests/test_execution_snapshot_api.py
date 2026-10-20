"""运行进度不应等待产物新鲜度或逐句文件扫描。"""
import datetime
import json

import pytest
from fastapi import Response
from sqlalchemy import event

from dubforge.domain import crud
from dubforge.domain.models import Job


def test_execution_snapshot_reads_runtime_without_artifact_checks(make_project, monkeypatch):
    from app.api.projects import project_execution_snapshot
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.runner import runner

    uid = make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'tts')
    step.status, step.progress = 'running', 22.6
    crud.save_step(step)
    job = Job(project_id=project.id, step_key='tts', execution=json.dumps(dict(
        schema=1, phase='tts.synthesizing', label='处理第 70 句配音',
        completed=69, total=305, unit='句')))
    with crud.session() as session:
        session.add(job); session.commit(); session.refresh(job)

    def forbidden(*args, **kwargs):
        raise AssertionError('轻量进度禁止读取产物/日志/配置或核对新鲜度')
    monkeypatch.setattr(Dependencies, '__init__', forbidden)
    monkeypatch.setattr(crud, 'latest_artifacts', forbidden)
    monkeypatch.setattr(crud, 'project_params', forbidden)
    monkeypatch.setattr(runner, 'progress_eta', lambda *_: dict(
        eta_seconds=14, progress_phase='服务正在生成', progress_indeterminate=False,
        execution={}))
    response = Response()
    result = project_execution_snapshot(response, f'{uid},{uid},missing')
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-dubforge-workspace'] == result['workspace_id']
    assert len(result['projects']) == 1
    snapshot = result['projects'][0]
    assert snapshot['uid'] == uid and snapshot['running_step'] == 'tts'
    tts = next(row for row in snapshot['steps'] if row['key'] == 'tts')
    assert tts['job_id'] == job.id and tts['progress'] == 22.6
    assert tts['execution']['completed'] == 69
    assert tts['execution']['label'] == '处理第 70 句配音'
    assert tts['eta_seconds'] == 14
    assert 'params' not in snapshot and 'artifacts' not in tts and 'fresh' not in tts


def test_execution_snapshot_terminal_identity_and_archive_scope(make_project):
    from app.api.projects import project_execution_snapshot

    uid, archived = make_project(), make_project()
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'tts')
    step.status, step.progress = 'done', 100
    crud.save_step(step)
    job = Job(project_id=project.id, step_key='tts', status='done', execution='{}')
    hidden = crud.get_project(archived)
    with crud.session() as session:
        hidden.archived_at = datetime.datetime.now(datetime.timezone.utc)
        session.add(hidden); session.add(job); session.commit(); session.refresh(job)
    snapshot = project_execution_snapshot(Response(), f'{uid},{archived}')['projects']
    assert [row['uid'] for row in snapshot] == [uid]
    tts = next(row for row in snapshot[0]['steps'] if row['key'] == 'tts')
    assert tts['status'] == 'done' and tts['job_id'] == job.id
    assert tts['progress_indeterminate'] is False and tts['eta_seconds'] == 0
    assert snapshot[0]['running_step'] is None


def test_execution_snapshot_batches_database_reads(make_project):
    from app.api.projects import project_execution_snapshot
    from dubforge.db import get_engine

    uids = [make_project() for _ in range(12)]
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)
    engine = get_engine()
    event.listen(engine, 'before_cursor_execute', record)
    try:
        result = project_execution_snapshot(Response(), ','.join(uids))
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert len(result['projects']) == 12
    assert len(statements) == 3, '项目数增加不能带来逐项目/逐句查询'


def test_execution_snapshot_bounds_scope(isolated_env):
    from app.api.projects import project_execution_snapshot
    from dubforge.errors import DubForgeError

    assert project_execution_snapshot(Response())['projects'] == []
    with pytest.raises(DubForgeError) as error:
        project_execution_snapshot(Response(), ','.join(str(index) for index in range(101)))
    assert error.value.code == 'PROJECT_EXECUTION_SCOPE_INVALID'


def test_execution_snapshot_route_precedes_project_lookup(isolated_env):
    from app.main import app
    from fastapi.testclient import TestClient
    # 不进入 lifespan，不启动工作流或真实服务。
    client = TestClient(app)
    response = client.get('/api/projects/execution')
    assert response.status_code == 200
    assert response.json()['projects'] == []
