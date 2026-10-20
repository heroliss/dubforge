"""恢复受理必须立即覆盖旧诊断；隔离队列，不执行模型或发布操作。"""
import json
import copy
import uuid
from contextlib import nullcontext

import pytest
from sqlmodel import Session

from dubforge import release_batches, workflows
from dubforge.domain import crud
from dubforge.db import get_engine
from dubforge.errors import DubForgeError
from dubforge.execution_progress import Recorder
from dubforge.publishing import service
from dubforge.workflow_models import WorkflowRun
from test_publishing import setup_publish
from test_workflows import flow, paused_recovery, recovery_body


def failed_execution(key):
    recorder = Recorder(owner='workflow', run_id=key, clock=lambda: 1000.)
    recorder.phase('production.tts', '旧配音失败', completed=69, total=305, unit='句',
                   metrics={'current_id': 70})
    return recorder.finish('failed', '旧请求没有结束，禁止重复提交')


def interrupted(flow, monkeypatch):
    uid, spec, key, _, _, _ = paused_recovery(flow, monkeypatch,
        steps=['download', 'assemble'], cursor=1,
        fresh_steps={'download': True, 'assemble': False})
    workflows._update(key, execution=failed_execution(key), error='旧失败')
    preview = workflows.preview(uid, {**spec, 'resume_id': key})
    return uid, key, recovery_body(preview, request_id=str(uuid.uuid4()))


def test_direct_resume_acceptance_and_status_replace_failed_execution_atomically(flow, monkeypatch):
    uid, key, body = interrupted(flow, monkeypatch)
    result = workflows.resume(key, body)
    for run in (result, workflows.status(uid)['run']):
        assert run['status'] == 'queued' and run['error'] == ''
        execution = run['execution']
        assert execution['state'] == 'queued'
        assert execution['phase'] == 'workflow.queued'
        assert execution['label'] == '恢复检查完成，等待执行'
        assert execution['detail'] == '' and execution['metrics'] == {}
        assert execution['completed'] is None and execution['total'] is None
        assert execution['finished_at'] is None
        assert not execution['started_at'].startswith('1970-'), '新一轮耗时不包含上次失败后的长时间暂停'
        assert execution['history'][-2]['state'] == 'failed'
        assert '旧请求' in execution['history'][-2]['detail']
        assert execution['history'][-1]['state'] == 'queued'
    assert not crud.latest_job(crud.get_project(uid).id, 'assemble'), '受理不能伪造真实制作任务'


def test_delayed_worker_acceptance_is_idempotent_and_keeps_lease(flow, monkeypatch):
    launch = workflows._launch
    uid, key, body = interrupted(flow, monkeypatch)
    monkeypatch.setattr(workflows, '_launch', launch)
    submitted = []
    monkeypatch.setattr(workflows._pool, 'submit', lambda *args: submitted.append(args))
    try:
        first = workflows.resume(key, body)
        second = workflows.resume(key, body)
        assert first['execution']['state'] == second['execution']['state'] == 'queued'
        assert len(submitted) == 1 and workflows._leases[uid] == key
        with pytest.raises(DubForgeError, match='不能继续'):
            workflows.resume(key, {**body, 'request_id': str(uuid.uuid4())})
        assert len(submitted) == 1
    finally:
        workflows._running.pop(key, None)
        workflows._leases.pop(uid, None)


def test_resume_queue_rejection_releases_lease_and_preserves_request_receipt(flow, monkeypatch):
    launch = workflows._launch
    uid, key, body = interrupted(flow, monkeypatch)
    monkeypatch.setattr(workflows, '_launch', launch)
    attempts = []
    def reject(*args):
        attempts.append(args)
        raise RuntimeError('queue closed')
    monkeypatch.setattr(workflows._pool, 'submit', reject)
    with pytest.raises(DubForgeError) as error:
        workflows.resume(key, body)
    assert error.value.code == 'WORKFLOW_QUEUE_UNAVAILABLE'
    assert key not in workflows._running and uid not in workflows._leases
    failed = workflows.get(key)
    assert failed['status'] == failed['execution']['state'] == 'failed'
    assert '后台队列' in failed['phase'] and '后台队列' in failed['error']
    assert body['request_id'] in failed['plan']['_resume_requests']
    replay = workflows.resume(key, body)
    assert replay['status'] == 'failed' and len(attempts) == 1, '旧受理请求不能再次入队'
    submitted = []
    monkeypatch.setattr(workflows._pool, 'submit', lambda *args: submitted.append(args))
    try:
        resumed = workflows.resume(key, {**body, 'request_id': str(uuid.uuid4())})
        assert resumed['status'] == resumed['execution']['state'] == 'queued'
        assert len(submitted) == 1
        assert len(resumed['plan']['_resume_requests']) == 2
    finally:
        workflows._running.pop(key, None)
        workflows._leases.pop(uid, None)


def test_receipted_publication_resume_replaces_failed_execution_without_republication(flow, setup_publish, monkeypatch):
    uid, key, body = interrupted(flow, monkeypatch)
    publication_id = setup_publish[1]['id']
    service._update(publication_id, status='published', receipt={'bvid': 'BV1234567890'})
    workflows._update(key, publication_id=publication_id)
    accepted = workflows.resume(key, body)
    assert accepted['status'] == 'monitoring'
    assert accepted['execution']['state'] != 'failed', '平台已发布的真实结果可以覆盖等待核对'
    with Session(get_engine()) as session:
        execution = json.loads(session.get(WorkflowRun, key).execution)
    assert execution['state'] == 'waiting'
    assert execution['label'] == '继续核对平台状态'
    assert execution['detail'] == ''
    assert execution['history'][-2]['state'] == 'failed'
    assert not any(op in {'upload', 'submit'} for op, _payload in setup_publish[3])
    assert not crud.latest_job(crud.get_project(uid).id, 'assemble')


def test_batch_reacceptance_preserves_direct_resume_receipts_and_old_uuid_cannot_launch_again(flow, monkeypatch):
    uid, key, body = interrupted(flow, monkeypatch)
    launched = []
    monkeypatch.setattr(workflows, '_launch', lambda *args: launched.append(args))
    accepted = workflows.resume(key, body)
    original_receipt = copy.deepcopy(accepted['plan']['_resume_requests'][body['request_id']])
    workflows._update(key, status='failed', error='第一次恢复之后失败')
    candidate = workflows.preview(uid, {**body['recipe'], 'resume_id': key})
    assert '_resume_requests' not in candidate, '公共检查方案不携带内部永久回执'
    fingerprint = candidate['fingerprint']
    monkeypatch.setattr(workflows, 'prepared_plan', lambda *_args, **_kwargs: copy.deepcopy(candidate))
    # 只替代批次身份边界，真实 start_prepared 保留锁、预检、重规划和落盘路径。
    monkeypatch.setattr(release_batches, 'owns', lambda selected: selected == uid)
    monkeypatch.setattr(release_batches, 'launching', lambda _uid: nullcontext())
    monkeypatch.setattr(release_batches, 'bind_workflow', lambda *_args: None)
    queued = workflows.start_prepared(uid, candidate, str(uuid.uuid4()), existing_id=key)
    assert queued['status'] == 'queued' and len(launched) == 2
    assert queued['plan']['fingerprint'] == fingerprint
    assert queued['plan']['_resume_requests'][body['request_id']] == original_receipt
    workflows._update(key, status='failed', error='批次接管之后再次失败')
    monkeypatch.setattr(workflows, '_plan', lambda *_args, **_kwargs: pytest.fail('旧回执重送不能再次预检或入队'))
    replay = workflows.resume(key, body)
    assert replay['status'] == 'failed' and len(launched) == 2
    with pytest.raises(DubForgeError) as error:
        workflows.resume(key, {**body, 'fingerprint': 'changed-payload'})
    assert error.value.code == 'WORKFLOW_CONFLICT' and len(launched) == 2
