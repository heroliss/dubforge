"""项目导航批次阶段使用轻量流程简要；全部数据隔离，不触发执行或外部读取。"""
import datetime
import json
import uuid

import pytest
from sqlalchemy import event
from sqlmodel import Session

from dubforge import release_batches, release_batch_progress, workflows
from dubforge.db import get_engine
from dubforge.publishing import service, transport
from dubforge.release_batch_models import ReleaseBatch
from dubforge.workflow_models import WorkflowRun


@pytest.fixture
def env(make_project, monkeypatch):
    uid, other = make_project(title='当前项目'), make_project(title='另一项目')
    with Session(get_engine()) as session:
        run = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='running', phase='字幕与配音',
                          plan='不是本接口需要读取的完整计划', recipe='不是本接口需要读取的完整配方')
        session.add(run); session.commit(); session.refresh(run)
        run_id = run.id
        item = dict(project_uid=uid, status='running', phase='正在核对已有结果与执行条件', workflow_id=run.id)
        batch = ReleaseBatch(request_id=str(uuid.uuid4()), status='running', phase='顺序处理项目',
                             items=json.dumps([item]), plan='本接口不能读取计划')
        session.add(batch); session.commit(); session.refresh(batch)

    def forbidden(*_args, **_kwargs):
        pytest.fail('项目阶段摘要不得读取完整流程、产物候选、执行详情或平台')

    for owner, name in [(workflows, 'get'), (service, 'view'), (service, 'candidates'),
                        (transport, 'call'), (release_batch_progress, 'load')]:
        monkeypatch.setattr(owner, name, forbidden)
    return dict(uid=uid, other=other, run_id=run_id, batch_id=batch.id, item=item)


def update(env, *, batch=None, item=None, run=None):
    with Session(get_engine()) as session:
        row = session.get(ReleaseBatch, env['batch_id'])
        for key, value in (batch or {}).items():
            setattr(row, key, value)
        if item:
            row.items = json.dumps([{**env['item'], **item}])
        session.add(row)
        current = session.get(WorkflowRun, env['run_id'])
        for key, value in (run or {}).items():
            setattr(current, key, value)
        session.add(current); session.commit()


def read(uids):
    statements = []
    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)
    event.listen(get_engine(), 'before_cursor_execute', capture)
    try:
        result = release_batches.project_statuses(uids)
    finally:
        event.remove(get_engine(), 'before_cursor_execute', capture)
    assert not any(token in statement.lower() for statement in statements
                   for token in ('workflowrun.plan', 'workflowrun.recipe', 'workflowrun.execution',
                                 'releasebatch.plan', 'artifact', 'steprun', ' job.'))
    return result, statements


def test_current_project_phase_follows_its_workflow_without_rewriting_batch(env):
    result, statements = read([env['uid']])
    assert result[env['uid']]['phase'] == '字幕与配音'
    assert result[env['uid']]['batch_phase'] == '顺序处理项目'
    assert result[env['uid']]['lock_scope'] == 'current'
    assert len(statements) == 2
    with Session(get_engine()) as session:
        stored = json.loads(session.get(ReleaseBatch, env['batch_id']).items)
    assert stored[0]['phase'] == '正在核对已有结果与执行条件'
    update(env, run=dict(phase='合成成片'))
    assert read([env['uid']])[0][env['uid']]['phase'] == '合成成片'


@pytest.mark.parametrize('batch,item,run,expected,queries', [
    ({}, {'status':'queued'}, {}, '等待前项完成', 1),
    ({}, {'feedback':'preparing'}, {}, '正在核对已有结果与执行条件', 1),
    ({'status':'paused'}, {}, {}, '批次已暂停，等待重新检查', 1),
    ({'status':'stopped'}, {}, {}, '批次已停止，已有结果保留', 1),
    ({'status':'attention'}, {}, {}, '批次等待处理，请核对项目当前状态', 1),
    ({'status':'failed'}, {}, {}, '正在核对已有结果与执行条件', 1),
    ({'status':'ready'}, {}, {}, '正在核对已有结果与执行条件', 1),
    ({}, {'publication':{'reuse_existing':True}}, {}, '正在核对已有结果与执行条件', 1),
    ({}, {'publication':'未知旧引用'}, {}, '正在核对已有结果与执行条件', 1),
    ({}, {'publication':[]}, {}, '正在核对已有结果与执行条件', 1),
    ({}, {'workflow_id':''}, {}, '正在核对已有结果与执行条件', 1),
    ({}, {'workflow_id':'missing'}, {}, '正在核对已有结果与执行条件', 2),
    ({}, {}, {'status':'paused'}, '正在核对已有结果与执行条件', 2),
    ({}, {}, {'status':'failed'}, '正在核对已有结果与执行条件', 2),
    ({}, {}, {'status':'attention'}, '正在核对已有结果与执行条件', 2),
    ({}, {}, {'status':'unknown'}, '正在核对已有结果与执行条件', 2),
    ({}, {}, {'status':'queued'}, '等待流程开始', 2),
    ({}, {'status':'monitoring'}, {'status':'monitoring','phase':'等待B站审核'}, '等待B站审核', 2),
    ({'status':'pausing'}, {}, {}, '字幕与配音', 2),
    ({'status':'stopping'}, {}, {'status':'stopping'}, '字幕与配音', 2),
])
def test_control_queue_preparation_reuse_and_unknown_boundaries(env, batch, item, run, expected, queries):
    update(env, batch=batch, item=item, run=run)
    result, statements = read([env['uid']])
    assert result[env['uid']]['phase'] == expected
    assert len(statements) == queries


def test_cross_project_reference_does_not_borrow_another_workflow_phase(env):
    update(env, run=dict(project_uid=env['other'], phase='另一项目的执行阶段'))
    result, statements = read([env['uid']])
    assert result[env['uid']]['phase'] == env['item']['phase']
    assert len(statements) == 2


def test_many_current_projects_use_one_batched_brief_query_and_newest_queued_membership_wins(env, make_project):
    uids = [env['uid']]
    created = [make_project(title=f'项目{index}') for index in range(20)]
    with Session(get_engine()) as session:
        for index, uid in enumerate(created):
            run = WorkflowRun(project_uid=uid, request_id=str(uuid.uuid4()), status='running', phase=f'当前阶段{index}')
            session.add(run); session.flush()
            session.add(ReleaseBatch(request_id=str(uuid.uuid4()), status='running',
                items=json.dumps([dict(project_uid=uid,status='running',phase='旧阶段',workflow_id=run.id)])))
            uids.append(uid)
        session.commit()
    result, statements = read(uids)
    assert len(result) == 21 and len(statements) == 2
    assert all(result[uid]['phase'] == f'当前阶段{index}' for index, uid in enumerate(uids[1:]))
    with Session(get_engine()) as session:
        session.add(ReleaseBatch(request_id=str(uuid.uuid4()), status='running',
            created_at=datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=1),
            items=json.dumps([dict(project_uid=env['uid'],status='queued',phase='过去错误',workflow_id=env['run_id'])])))
        session.commit()
    result, statements = read([env['uid']])
    assert result[env['uid']]['phase'] == '等待前项完成' and len(statements) == 1


def test_malformed_or_irrelevant_records_do_not_query_workflows(env):
    for items in ('not-json', '{}', '[null,1,{"project_uid":4}]'):
        update(env, batch=dict(items=items))
        result, statements = read([env['uid']])
        assert result == {} and len(statements) == 1
    assert read([]) == ({}, [])
