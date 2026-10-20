"""顺序发布的当前阶段进度：隔离数据，轻量批量读，不读取产物或调用平台。"""
import json
import uuid

import pytest
from sqlalchemy import event
from sqlmodel import Session

from dubforge import release_batches, release_batch_progress
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.publishing import service, transport
from dubforge.publishing.models import Publication
from dubforge.release_batch_models import ReleaseBatch
from dubforge.runner import runner
from dubforge.workflow_models import WorkflowRun


@pytest.fixture()
def env(make_project, monkeypatch):
    uid = make_project(title='首个项目')
    other = make_project(title='后续项目')
    monkeypatch.setattr(runner, 'progress_eta', lambda *_args: {})
    with Session(get_engine()) as session:
        run = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='running', cursor=2,
            phase='语义整理', plan=json.dumps(dict(steps=['download', 'transcribe', 'split', 'translate', 'tts', 'assemble'])))
        session.add(run)
        session.commit()
        session.refresh(run)
        batch = ReleaseBatch(request_id=str(uuid.uuid4()), status='running', cursor=0, phase='第1/2项：首个项目',
            scope=json.dumps(dict(account_id='123', season_id=2, section_id=3)),
            items=json.dumps([dict(project_uid=uid, title='首个项目', status='running', phase='制作中', workflow_id=run.id),
                              dict(project_uid=other, title='后续项目', status='queued', phase='历史失败', error='旧错误')]))
        session.add(batch)
        session.commit()
        session.refresh(batch)
        session.refresh(run)
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'split')
    step.status, step.progress, step.eta_seconds = 'running', 37.5, 123
    crud.save_step(step)
    def forbidden(*_args, **_kwargs):
        pytest.fail('读取进度不得调用完整发布详情、流程详情、媒体候选或远端')
    monkeypatch.setattr(service, 'view', forbidden)
    monkeypatch.setattr(service, 'candidates', forbidden)
    monkeypatch.setattr(transport, 'call', forbidden)
    monkeypatch.setattr(release_batches.workflows, 'get', forbidden)
    return dict(uid=uid, other=other, batch=batch, run=run, step=step)


def change_batch(env, **fields):
    with Session(get_engine()) as session:
        row = session.get(ReleaseBatch, env['batch'].id)
        for name, value in fields.items():
            setattr(row, name, json.dumps(value) if name in {'items', 'plan', 'scope'} else value)
        session.add(row)
        session.commit()


def change_run(env, **fields):
    with Session(get_engine()) as session:
        row = session.get(WorkflowRun, env['run'].id)
        for name, value in fields.items():
            setattr(row, name, json.dumps(value) if name == 'plan' else value)
        session.add(row)
        session.commit()


def current(env):
    return release_batches.get(env['batch'].id)['current_item']


def set_publication(env, status, *, progress=0, phase=None):
    with Session(get_engine()) as session:
        pub = Publication(project_uid=env['uid'], status=status, progress=progress,
            snapshot=json.dumps(dict(parts=[dict(artifact_id=1, size=100), dict(artifact_id=2, size=300)])),
            transfers=json.dumps({'_progress': dict(phase=phase or status, loaded=progress * 4,
                                                    current_part=1, current_title='第一个分P')}))
        session.add(pub)
        session.commit()
        session.refresh(pub)
        row = session.get(WorkflowRun, env['run'].id)
        row.cursor, row.publication_id = 6, pub.id
        row.phase = '上传并投稿'
        session.add(row)
        session.commit()
        return pub.id


def test_first_project_progress_moves_while_completed_project_count_is_still_zero(env):
    before = release_batches.get(env['batch'].id)
    value = before['current_item']
    assert before['cursor'] == 0 and before['total'] == 2
    assert value['position'] == 1 and value['project_uid'] == env['uid']
    assert value['stage'] == 'production' and value['stage_label'] == '语义整理'
    assert value['percentage'] == 37.5 and not value['indeterminate']
    assert value['completed_steps'] == 2 and value['total_steps'] == 6
    assert value['eta_seconds'] == 123 and not value['estimated']
    assert '37.5%' in value['progress_label']
    step = env['step']
    step.progress = 52
    crud.save_step(step)
    assert current(env)['percentage'] == 52
    summary = release_batches.listing()['batches'][0]
    assert summary['current_item'] == current(env)
    assert summary['cursor'] == 0
    assert summary['items'][1]['phase'] == '等待前项完成' and summary['items'][1]['error'] == ''


def test_llm_live_estimated_progress_and_phase_remain_explicit(env, monkeypatch):
    monkeypatch.setattr(runner, 'progress_eta', lambda *_args: dict(progress=63.2, progress_estimated=True,
        progress_phase='输出最终文本', progress_indeterminate=False, eta_seconds=85))
    value = current(env)
    assert value['percentage'] == 63.2 and value['estimated']
    assert value['phase'] == '输出最终文本' and value['eta_seconds'] == 85
    assert '预计63.2%' in value['progress_label']


@pytest.mark.parametrize('progress', [0, float('nan'), float('inf'), -10])
def test_no_default_zero_or_invalid_progress_is_presented_as_measured(env, monkeypatch, progress):
    monkeypatch.setattr(runner, 'progress_eta', lambda *_args: dict(progress=progress,
        eta_seconds=float('inf'), progress_phase='等待模型返回', progress_indeterminate=True))
    value = current(env)
    assert value['percentage'] is None and value['eta_seconds'] is None
    assert value['indeterminate'] and '等待模型返回' in value['progress_label']


def test_default_zero_without_live_measurement_is_indeterminate(env):
    step = env['step']
    step.progress, step.eta_seconds = 0, 0
    crud.save_step(step)
    value = current(env)
    assert value['percentage'] is None and value['indeterminate']


def test_live_preparation_phase_hides_old_recorded_percentage(env, monkeypatch):
    monkeypatch.setattr(runner, 'progress_eta', lambda *_args: dict(progress_indeterminate=True,
        progress_phase='加载语音模型', eta_seconds=0))
    value = current(env)
    assert value['percentage'] is None and value['indeterminate']
    assert value['phase'] == '加载语音模型'


@pytest.mark.parametrize('phase', ['uploading', 'merging'])
def test_upload_percentage_comes_from_real_transferred_progress(env, phase):
    set_publication(env, 'uploading', progress=27.5, phase=phase)
    value = current(env)
    assert value['stage'] == 'upload' and value['percentage'] == 27.5
    assert value['completed_steps'] == value['total_steps'] == 6
    assert '第1/2P' in value['progress_label'] and '27.5%' in value['progress_label']
    assert not value['indeterminate'] and not value['estimated']


@pytest.mark.parametrize('status,phase,stage', [('uploading', 'cover', 'preparing'),
    ('queued', 'validating', 'preparing'), ('submitting', None, 'submission'),
    ('joining', None, 'collection'), ('checking', None, 'review'), ('submitted', None, 'review'),
    ('published', None, 'verifying'), ('uploaded', None, 'submission')])
def test_unmeasurable_submission_review_and_collection_do_not_claim_100_percent(env, status, phase, stage):
    set_publication(env, status, progress=100, phase=phase)
    value = current(env)
    assert value['stage'] == stage
    assert value['percentage'] is None and value['indeterminate']


def test_current_step_queued_waits_without_reusing_stale_percentage(env):
    step = env['step']
    step.status, step.progress = 'queued', 80
    crud.save_step(step)
    value = current(env)
    assert value['percentage'] is None and value['indeterminate']
    assert value['phase'] == '等待任务开始'


def test_comments_have_explicit_unknown_progress_without_a_step_row(env):
    change_run(env, cursor=1, phase='采集、精选并翻译上屏评论', plan={'steps': ['download', 'comments', 'assemble']})
    value = current(env)
    assert value['stage_label'] == '采集并处理评论'
    assert value['percentage'] is None and value['indeterminate']
    assert value['completed_steps'] == 1 and value['total_steps'] == 3


def test_preparing_draft_after_production_is_a_separate_phase(env):
    change_run(env, cursor=6, phase='准备发布资料')
    value = current(env)
    assert value['stage'] == 'preparing' and value['stage_label'] == '准备发布资料'
    assert value['completed_steps'] == 6 and value['percentage'] is None


def test_review_projection_includes_next_workflow_check_without_more_queries(env):
    set_publication(env, 'submitted', progress=100)
    change_run(env, status='monitoring', next_check=1234567890.)
    assert current(env)['execution']['next_check_at'] == 1234567890.


def test_reusing_original_bv_does_not_import_old_workflow_failure_or_production_progress(env):
    pub = set_publication(env, 'published', progress=100)
    change_run(env, status='failed', cursor=0, phase='上次旧错误')
    change_batch(env, items=[dict(project_uid=env['uid'], title='首个项目', status='running',
        publication_id=pub, workflow_id=env['run'].id, publication={'reuse_existing': True})])
    value = current(env)
    assert value['stage'] == 'verifying' and value['stage_label'] == '核对原BV与合集'
    assert value['percentage'] is None and value['total_steps'] == 0
    assert '旧错误' not in value['phase']


def test_waiting_current_item_does_not_load_old_workflow_or_replace_selected_identity(env):
    change_run(env, status='failed', phase='历史失败步骤', publication_id='old-upload')
    change_batch(env, items=[dict(project_uid=env['uid'], title='首个项目', status='queued',
        workflow_id=env['run'].id, publication_id='selected-review-draft', phase='旧阶段', error='旧失败')])
    detail = release_batches.get(env['batch'].id)
    assert detail['items'][0]['publication_id'] == 'selected-review-draft'
    assert detail['items'][0]['phase'] == '等待前项完成'
    assert detail['items'][0]['error'] == ''
    assert detail['current_item']['stage'] == 'waiting'
    assert detail['current_item']['percentage'] is None


@pytest.mark.parametrize('status', ['ready', 'paused', 'attention', 'stopped', 'completed'])
def test_inactive_batches_have_no_current_active_progress_and_no_child_table_reads(env, status):
    change_batch(env, status=status)
    statements = []
    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)
    event.listen(get_engine(), 'before_cursor_execute', observe)
    try:
        assert current(env) is None
    finally:
        event.remove(get_engine(), 'before_cursor_execute', observe)
    assert len(statements) == 1


def test_final_sorting_remains_active_after_all_projects_finished(env):
    change_batch(env, cursor=2, phase='核对并整理合集顺序', items=[
        dict(project_uid=env['uid'], title='首个项目', status='done'),
        dict(project_uid=env['other'], title='后续项目', status='done')])
    value = current(env)
    assert value['position'] is None and value['project_uid'] is None
    assert value['stage'] == 'ordering' and value['percentage'] is None
    assert value['indeterminate'] and value['stage_label'] == '核对并整理合集顺序'


def test_list_query_count_stays_bounded_with_many_historical_or_waiting_rows(env):
    with Session(get_engine()) as session:
        for index in range(40):
            session.add(ReleaseBatch(request_id=str(uuid.uuid4()), status='paused' if index % 2 else 'ready',
                items=json.dumps([dict(project_uid=env['other'], title='历史项目', status='queued',
                    workflow_id='unread-history-' + str(index), publication_id='unread-publication-' + str(index))])))
        session.commit()
    statements = []
    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)
    event.listen(get_engine(), 'before_cursor_execute', observe)
    try:
        result = release_batches.listing()['batches']
    finally:
        event.remove(get_engine(), 'before_cursor_execute', observe)
    assert len(result) == 41
    assert len(statements) == 3
    assert sum(row['current_item'] is not None for row in result) == 1
    assert not any('artifact' in statement.lower() or 'job.' in statement.lower() for statement in statements)


def test_cross_project_workflow_and_publication_refs_do_not_leak_current_progress(env):
    change_run(env, project_uid=env['other'], publication_id='wrong-project-record')
    value = current(env)
    assert value['percentage'] is None and value['total_steps'] == 0
    assert value['project_uid'] == env['uid']
    detail = release_batches.get(env['batch'].id)
    assert detail['items'][0].get('publication_id') != 'wrong-project-record'


def test_http_list_and_detail_share_current_item_contract(env):
    from app.main import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    detail = client.get('/api/release-batches/' + env['batch'].id)
    listing = client.get('/api/release-batches')
    assert detail.status_code == listing.status_code == 200
    assert detail.json()['current_item'] == listing.json()['batches'][0]['current_item']
    assert detail.json()['current_item']['percentage'] == 37.5
