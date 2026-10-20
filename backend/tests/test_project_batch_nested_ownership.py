"""两层批次授权回归：保留真实流程线程和 Runner，仅替换物理下载内容。"""
import copy
import json
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from dubforge import project_batches, release_batches, workflows
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.project_batch_models import ProjectBatch
from dubforge.publishing import service
from dubforge.release_batch_models import ReleaseBatch
from dubforge.workflow_models import WorkflowRun
from dubforge.workflow_plan import digest


@pytest.fixture
def nested_batch(make_project, monkeypatch):
    """父批次、合集子批次保留真实存储身份，绝不启动平台执行器。"""
    uid = make_project(title='两层批次中的项目')
    parent_id, child_id, group_id = [uuid.uuid4().hex for _ in range(3)]
    group = dict(id=group_id, kind='collection', title='隔离合集', status='running',
                 child_id=child_id, project_uids=[uid], items=[dict(project_uid=uid)])
    with Session(get_engine()) as session:
        session.add(ProjectBatch(id=parent_id, request_id=str(uuid.uuid4()), status='running',
                                 groups=json.dumps([group])))
        session.add(ReleaseBatch(id=child_id, request_id=str(uuid.uuid4()), status='running',
            plan=json.dumps(dict(parent_batch_id=parent_id)),
            items=json.dumps([dict(project_uid=uid, title='两层批次中的项目', status='running')])))
        session.commit()
    for module in (project_batches, release_batches, workflows):
        monkeypatch.setattr(module, '_leases', {})
        monkeypatch.setattr(module, '_running', {})
        monkeypatch.setattr(module, '_owner', threading.local())
    for module in (project_batches, release_batches):
        monkeypatch.setattr(module, '_children', {})
        monkeypatch.setattr(module, '_closing', False)
    project_batches._leases[uid] = parent_id
    project_batches._running[(parent_id, group_id)] = threading.Event()
    release_batches._leases[uid] = child_id
    release_batches._running[child_id] = threading.Event()
    return SimpleNamespace(uid=uid, parent_id=parent_id, child_id=child_id, group=group)


def _workflow_identity(env):
    """模拟子批次已经绑定工作流后，实际工作流线程具有的精确身份。"""
    key = uuid.uuid4().hex
    with Session(get_engine()) as session:
        session.add(WorkflowRun(id=key, project_uid=env.uid, request_id=str(uuid.uuid4())))
        session.commit()
    release_batches._children[env.uid] = key
    workflows._leases[env.uid] = key
    return key


def test_nested_workflow_identity_passes_all_project_start_guards(nested_batch):
    env = nested_batch
    key = _workflow_identity(env)
    assert env.uid not in project_batches._children, '合集工作流只在子批次中登记'
    with workflows._owned(key):
        assert not project_batches.owns(env.uid)
        assert not release_batches.owns(env.uid)
        assert project_batches.child_owned(env.uid), '合法孙流程必须继承精确的项目占用权限'
        project_batches.check_idle(env.uid)
        workflows.check_idle(env.uid)
        service.check_project_idle(env.uid)


@pytest.mark.parametrize('identity', ['external', 'unrelated_workflow'])
def test_external_and_unrelated_workflow_cannot_borrow_nested_lease(nested_batch, identity):
    env = nested_batch
    _workflow_identity(env)
    key = None if identity == 'external' else uuid.uuid4().hex
    with workflows._owned(key):
        assert not project_batches.child_owned(env.uid)
        with pytest.raises(DubForgeError) as caught:
            project_batches.check_idle(env.uid)
        assert caught.value.code == 'PROJECT_BATCH_BUSY'


@pytest.mark.parametrize('broken', [
    'release_lease_missing', 'workflow_binding_missing', 'wrong_workflow_binding',
    'wrong_child_id', 'wrong_project_uid', 'independent_group', 'paused_group',
    'paused_parent', 'paused_child', 'wrong_child_parent',
])
def test_nested_identity_requires_the_exact_active_collection_group(nested_batch, broken):
    env = nested_batch
    key = _workflow_identity(env)
    if broken == 'release_lease_missing':
        release_batches._leases.pop(env.uid)
    elif broken == 'workflow_binding_missing':
        release_batches._children.pop(env.uid)
    elif broken == 'wrong_workflow_binding':
        release_batches._children[env.uid] = uuid.uuid4().hex
    elif broken in {'paused_parent', 'paused_child', 'wrong_child_parent'}:
        with Session(get_engine()) as session:
            if broken == 'paused_parent':
                row = session.get(ProjectBatch, env.parent_id)
                row.status = 'paused'
            else:
                row = session.get(ReleaseBatch, env.child_id)
                if broken == 'paused_child':
                    row.status = 'paused'
                else:
                    row.plan = json.dumps(dict(parent_batch_id=uuid.uuid4().hex))
            session.add(row)
            session.commit()
    else:
        group = copy.deepcopy(env.group)
        if broken == 'wrong_child_id':
            group['child_id'] = uuid.uuid4().hex
        elif broken == 'wrong_project_uid':
            group['project_uids'] = ['another-project']
        elif broken == 'independent_group':
            group['kind'] = 'independent'
        elif broken == 'paused_group':
            group['status'] = 'paused'
        with Session(get_engine()) as session:
            row = session.get(ProjectBatch, env.parent_id)
            row.groups = json.dumps([group])
            session.add(row)
            session.commit()
    with workflows._owned(key):
        assert not project_batches.child_owned(env.uid)
        with pytest.raises(DubForgeError) as caught:
            project_batches.check_idle(env.uid)
        assert caught.value.code == 'PROJECT_BATCH_BUSY'


@pytest.mark.parametrize('owner_rule', ['legacy', 'fixed'])
def test_real_nested_workflow_thread_enters_real_runner_download(nested_batch, monkeypatch, owner_rule):
    """不替换 _launch/_execute/start_step；旧错误应在真实下载门禁处复现。"""
    from dubforge.pipeline import steps
    from dubforge.runner import runner

    env = nested_batch
    entered = []

    def physical_download(ctx):
        entered.append(dict(project_uid=ctx.project.uid, job_id=ctx.job_id))
        ctx.write_json('nested_download_result.json', dict(isolated=True))

    # 只替换最后的物理步骤，真实队列、租约、资源准入、产物登记和收尾继续执行。
    monkeypatch.setitem(steps._RUNNERS, 'download', physical_download)
    spec = dict(mode='produce', variants=[])
    candidate = dict(recipe=spec, steps=['download'], labels={'download': '获取视频'},
        fingerprint=digest(spec), blockers=[], warnings=[], settings={})
    monkeypatch.setattr(workflows, 'prepared_plan', lambda *args, **kwargs: copy.deepcopy(candidate))

    def forbid_publication(*args, **kwargs):
        pytest.fail('两层占用回归禁止进入真实发布路径')

    monkeypatch.setattr(workflows, '_prepare_publication', forbid_publication)
    fixed_child_owned = project_batches.child_owned
    if owner_rule == 'legacy':
        # 精确恢复旧门禁，证明同一个真实线程/Runner 场景会复现原始错误。
        def legacy_child_owned(uid):
            direct = project_batches._children.get(uid)
            return bool(direct and direct == getattr(workflows._owner, 'key', None)) or release_batches.owns(uid)
        monkeypatch.setattr(project_batches, 'child_owned', legacy_child_owned)
    with release_batches._owned(env.child_id):
        row = workflows.start_prepared(env.uid, candidate, str(uuid.uuid4()), frozen_target=True)
    deadline = time.monotonic() + 15
    while row['id'] in workflows._running and time.monotonic() < deadline:
        time.sleep(.01)
    assert row['id'] not in workflows._running, '真实工作流线程必须完成收尾'
    result = workflows.get(row['id'])
    if owner_rule == 'legacy':
        assert result['status'] == 'failed'
        assert result['phase'] == '获取视频'
        assert result['cursor'] == 0
        assert result['error'] == '项目属于正在执行的批量任务，请先暂停或停止相应分组'
        assert not entered, '旧门禁在真正创建下载 Job 前已拒绝自己的孙流程'
        assert crud.latest_job(crud.get_project(env.uid).id, 'download') is None
        # 修复后从同一条失败流程继续，不能另外创建重复的流程或稿件。
        monkeypatch.setattr(project_batches, 'child_owned', fixed_child_owned)
        with release_batches._owned(env.child_id):
            resumed = workflows.start_prepared(env.uid, candidate, str(uuid.uuid4()),
                                              existing_id=row['id'], frozen_target=True)
        assert resumed['id'] == row['id']
        deadline = time.monotonic() + 15
        while row['id'] in workflows._running and time.monotonic() < deadline:
            time.sleep(.01)
        assert row['id'] not in workflows._running
        result = workflows.get(row['id'])
    assert result['status'] == 'completed', result.get('error')
    assert entered and entered[0]['project_uid'] == env.uid
    project = crud.get_project(env.uid)
    assert crud.get_step(project.id, 'download').status == 'done'
    assert crud.latest_job(project.id, 'download').status == 'done'
    assert runner.current_job_id(project.id, 'download') is None
    assert result['cursor'] == 1
    # 任务完成也不会让外部操作借用仍由父批次持有的项目租约。
    with pytest.raises(DubForgeError) as caught:
        project_batches.check_idle(env.uid)
    assert caught.value.code == 'PROJECT_BATCH_BUSY'
