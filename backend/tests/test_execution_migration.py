"""执行诊断加列不能恢复用户已停止的发布任务，也不能改写历史回执。"""
import json

from sqlmodel import Session, select

from dubforge import db, workflows
from dubforge.domain import crud
from dubforge.domain.models import Job
from dubforge.publishing.models import Publication
from dubforge.workflow_models import WorkflowRun


MONITOR_STOP = '已停止自动查询；不会撤回B站稿件'
RUNNING_STOP = '正在停止；已发送的投稿将等待回执'


def _rows(table):
    with db.get_engine().connect() as conn:
        return {row['id']: dict(row) for row in conn.exec_driver_sql(
            f'SELECT * FROM {table} ORDER BY id').mappings()}


def _seed(uid, cases):
    project = crud.get_project(uid)
    with Session(db.get_engine()) as session:
        session.add(Job(project_id=project.id, step_key='assemble', status='done',
            task_data='{"versions":["dub"],"completed":2}', log_path='logs/assemble.log',
            error_detail='已保留诊断'))
        for key, status, phase in cases:
            session.add(WorkflowRun(id=key, request_id=f'request-{key}', project_uid=uid,
                status=status, phase=phase, publication_id=f'pub-{key}', cursor=4,
                recipe='{"mode":"publish"}', plan='{"steps":["assemble"]}',
                error='历史错误', next_check=120, collection_attempts=2, check_failures=1))
            session.add(Publication(id=f'pub-{key}', project_uid=uid, status='published',
                draft='{"title":"已发布视频"}',
                snapshot=json.dumps({'chapter_options': {'enabled': True,
                    'items': [{'title': '章节', 'start_time': 12.5}]}}),
                transfers='{"video":{"cid":456}}',
                receipt='{"aid":123,"bvid":"BV1234567890","videos":[{"cid":456}]}',
                chapter_sync='{"status":"unknown","attempts":2,"error":"等待核对"}',
                progress=100))
        session.commit()


def _remove_new_columns():
    with db.get_engine().begin() as conn:
        conn.exec_driver_sql('ALTER TABLE job DROP COLUMN execution')
        conn.exec_driver_sql('ALTER TABLE workflowrun DROP COLUMN execution')
        conn.exec_driver_sql('ALTER TABLE workflowrun DROP COLUMN post_processing_paused')


def test_old_execution_tables_add_defaults_preserving_history_and_explicit_stop(make_project):
    uid = make_project()
    _seed(uid, [
        ('monitor-stop', 'attention', MONITOR_STOP),
        ('running-stop', 'stopping', RUNNING_STOP),
        ('attention-failure', 'attention', '章节同步失败'),
        ('near-phase', 'attention', MONITOR_STOP + '，请重试'),
        ('wrong-status', 'monitoring', MONITOR_STOP),
        ('wrong-stopping-status', 'paused', RUNNING_STOP),
        ('ordinary-interrupted', 'paused', '程序重启，全流程已暂停'),
        ('completed', 'completed', '全部完成'),
    ])
    _remove_new_columns()
    old_jobs, old_flows, old_publications = _rows('job'), _rows('workflowrun'), _rows('publication')

    db._ensure_columns()

    jobs, flows = _rows('job'), _rows('workflowrun')
    assert _rows('publication') == old_publications
    for key, previous in old_jobs.items():
        assert {name: jobs[key][name] for name in previous} == previous
        assert jobs[key]['execution'] == '{}'
    for key, previous in old_flows.items():
        assert {name: flows[key][name] for name in previous} == previous
        assert flows[key]['execution'] == '{}'
        assert bool(flows[key]['post_processing_paused']) is (key in {'monitor-stop', 'running-stop'})
    assert workflows.post_processing_paused('pub-monitor-stop') is True
    assert workflows.post_processing_paused('pub-running-stop') is True
    assert workflows.post_processing_paused('pub-attention-failure') is False

    # SQL 默认值也必须生效；旧写入者省略新列时不能得到 NULL。
    with db.get_engine().begin() as conn:
        for table, previous in [('job', next(iter(old_jobs.values()))),
                                ('workflowrun', old_flows['attention-failure'])]:
            values = dict(previous)
            values['id'] = 999 if table == 'job' else 'new-flow'
            if table == 'workflowrun':
                values['request_id'] = 'new-request'
            columns = ','.join(values)
            placeholders = ','.join('?' for _ in values)
            conn.exec_driver_sql(f'INSERT INTO {table} ({columns}) VALUES ({placeholders})', tuple(values.values()))
    assert _rows('job')[999]['execution'] == '{}'
    added = _rows('workflowrun')['new-flow']
    assert added['execution'] == '{}'
    assert added['post_processing_paused'] == 0


def test_stop_backfill_is_idempotent_with_existing_columns_and_keeps_diagnostics(make_project):
    uid = make_project()
    _seed(uid, [('stopped', 'attention', MONITOR_STOP), ('failed', 'attention', '章节同步失败')])
    # 模拟已经加列、尚未回填就退出；保留此前写入的新诊断。
    with Session(db.get_engine()) as session:
        for row in session.exec(select(WorkflowRun)).all():
            row.execution = '{"phase":{"label":"等待核对"},"history":[{"code":"publish.review"}]}'
            session.add(row)
        job = session.exec(select(Job)).one()
        job.execution = '{"phase":{"label":"成片已保存"}}'
        session.add(job)
        session.commit()
    jobs, flows, publications = _rows('job'), _rows('workflowrun'), _rows('publication')

    db._ensure_columns()
    db._ensure_columns()

    flows['stopped']['post_processing_paused'] = 1
    assert _rows('workflowrun') == flows
    assert _rows('job') == jobs
    assert _rows('publication') == publications


def test_explicit_resume_after_legacy_stop_is_not_paused_again(make_project):
    uid = make_project()
    _seed(uid, [('stopped', 'attention', MONITOR_STOP), ('running', 'stopping', RUNNING_STOP)])
    db._ensure_columns()
    publications = _rows('publication')
    # 启动恢复会把遗留的停止中任务置为暂停；只有显式继续才解除后处理门禁。
    workflows.recover()
    assert workflows.post_processing_paused('pub-running') is True
    workflows.resume_post_processing('pub-stopped')
    workflows.resume_post_processing('pub-running')
    resumed = _rows('workflowrun')
    assert resumed['stopped']['status'] == 'monitoring'
    assert resumed['stopped']['phase'] == '继续发布后处理'
    assert resumed['running']['status'] == 'paused'

    db._ensure_columns()

    assert _rows('workflowrun') == resumed
    assert workflows.post_processing_paused('pub-stopped') is False
    assert workflows.post_processing_paused('pub-running') is False
    assert _rows('publication') == publications
