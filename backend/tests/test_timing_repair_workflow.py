"""旧音频冲突穿过完整协调器和恢复链后修复；真实任务执行，模型与成片隔离。"""
import json
import time
from uuid import uuid4

import pytest

from conftest import mark_upstream_done
from test_automatic_timing_repair import old_conflict
from test_configuration import save
from test_dubbing_preparation import index_prepared, prepared
from test_voice_timing_integration import report
from dubforge import chain_recovery, workflows
from dubforge.backends import tts_indextts25
from dubforge.domain import crud
from dubforge.pipeline import dubbing_edit, voice_timing
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.tts_duration import audio_seconds
from dubforge.runner import runner


@pytest.fixture
def repair_flow(old_conflict, monkeypatch, stub_heavy_steps):
    client, uid, calls = old_conflict
    mark_upstream_done(uid, 'assemble')
    save('project:' + uid, {'run.mode': 'produce', 'content_review.enabled': False,
        'assembly.versions': [dict(id='dubbed', name='隔离配音版', dubbing=True,
            burn_subtitles=False, include_comments=False, overrides={})],
        'publish.version_ids': ['dubbed']})
    monkeypatch.setattr(tts_indextts25, 'is_online', lambda *_: True)
    stub_heavy_steps('assemble')
    from dubforge.pipeline import steps
    assemble = steps._RUNNERS['assemble']
    assembled = []

    def check_then_assemble(ctx):
        if any(version['dubbing'] for version in ctx.task_data['version_specs']):
            deps = Dependencies(crud.get_project(uid))
            layout = voice_timing.plan(deps.project, deps.root, deps.production_lines(), deps.cfg)
            assert layout['timing']['ready'], layout['timing']
            assert audio_seconds(dubbing_edit.take_path(deps.root, 1, 1)) == pytest.approx(1.3)
        assembled.append(ctx.step_key)
        return assemble(ctx)

    monkeypatch.setitem(steps._RUNNERS, 'assemble', check_then_assemble)
    from dubforge.publishing import service

    def no_publication(*args, **kwargs):
        pytest.fail('配音修复制作流程不应访问发布、上传或投稿')

    monkeypatch.setattr(service, 'enqueue', no_publication)
    monkeypatch.setattr(workflows, '_prepare_publication', no_publication)
    assert Dependencies(crud.get_project(uid)).fresh('tts')
    return client, uid, calls, assembled


def finish_workflow(key):
    deadline = time.monotonic() + 15
    while key in workflows._running and time.monotonic() < deadline:
        time.sleep(.01)
    assert key not in workflows._running
    return workflows.get(key)


def finish_chain(uid):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        project = crud.get_project(uid)
        if (not crud.project_params(project).get('_chain')
                and all(runner.current_job_id(project.id, step) is None for step in ('tts', 'assemble'))):
            return
        held = crud.project_params(project).get('_chain_hold')
        assert not held, held
        time.sleep(.01)
    raise TimeoutError('隔离恢复链未完成')


def workflow_intent(explicit=True, repair_timing=True):
    body = dict(mode='produce', production_steps=['assemble'])
    if explicit:
        body['dubbing'] = dict(mode='fill', repair_timing=repair_timing)
    return body


def held_chain(uid):
    project = crud.get_project(uid)
    params = crud.project_params(project)
    params.update(_chain=['assemble'], _chain_state='held',
                  _chain_hold=dict(step='assemble', code='STEP_INTERRUPTED', message='隔离测试待恢复'))
    project.params = json.dumps(params)
    crud.save_project(project)


@pytest.mark.parametrize('explicit', [True, False])
def test_repair_only_full_workflow_executes_tts_before_assembly_even_when_audio_is_fresh(repair_flow, explicit):
    client, uid, calls, assembled = repair_flow
    base = f'/api/projects/{uid}/workflow'
    previous_job = crud.latest_job(crud.get_project(uid).id, 'tts').id
    response = client.post(base + '/plan', json=workflow_intent(explicit))
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan['dubbing']['generate'] == 0 and plan['dubbing']['repair'] == 1
    assert not plan['blockers'], plan['blockers']
    assert plan['steps'] == ['tts', 'assemble']
    assert calls == [('你好', 1.1), ('世界', 1.1)]
    response = client.post(base + '/start', json=dict(recipe=plan['recipe'],
        fingerprint=plan['fingerprint'], confirmed=True, request_id=str(uuid4())))
    assert response.status_code == 200, response.text
    result = finish_workflow(response.json()['id'])
    assert result['status'] == 'completed', result
    assert result['cursor'] == 2 and not result['publication_id']
    assert calls == [('你好', 1.1), ('世界', 1.1), ('你好', .7)]
    assert crud.latest_job(crud.get_project(uid).id, 'tts').id != previous_job
    assert assembled == ['assemble'] and report(uid)['ready']
    assert dubbing_edit.take_ids(crud.project_dir(uid), 1) == [1]


def test_repair_only_recovery_reinserts_fresh_tts_and_runs_real_chain_before_assembly(repair_flow):
    client, uid, calls, assembled = repair_flow
    held_chain(uid)
    candidate = chain_recovery.plan(uid)
    assert candidate['dubbing']['generate'] == 0 and candidate['dubbing']['repair'] == 1
    assert candidate['steps'] == ['tts', 'assemble']
    assert candidate['recovery']['start_step'] == 'tts'
    assert candidate['recovery']['execute_steps'] == ['tts', 'assemble']
    assert not candidate['blockers'], candidate['blockers']
    response = client.post(f'/api/projects/{uid}/chain/resume', json=dict(
        fingerprint=candidate['fingerprint'], confirmed=True, request_id=str(uuid4())))
    assert response.status_code == 200, response.text
    finish_chain(uid)
    assert calls == [('你好', 1.1), ('世界', 1.1), ('你好', .7)]
    assert assembled == ['assemble'] and report(uid)['ready']


@pytest.mark.parametrize('explicit', [True, False])
def test_project_space_checked_assembly_adds_and_executes_repair_only_tts(repair_flow, explicit):
    client, uid, calls, assembled = repair_flow
    body = dict(steps=['assemble'], include_dependencies=True)
    if explicit:
        body['dubbing'] = dict(mode='fill', repair_timing=True)
    response = client.post(f'/api/projects/{uid}/run_plan', json=body)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert candidate['dubbing']['generate'] == 0 and candidate['dubbing']['repair'] == 1
    assert candidate['steps'] == ['tts', 'assemble']
    assert not candidate['blockers'], candidate['blockers']
    response = client.post(f'/api/projects/{uid}/run_chain', json={**body,
        'steps': candidate['steps'], 'plan_fingerprint': candidate['fingerprint']})
    assert response.status_code == 200, response.text
    finish_chain(uid)
    assert calls == [('你好', 1.1), ('世界', 1.1), ('你好', .7)]
    assert assembled == ['assemble'] and report(uid)['ready']


@pytest.mark.parametrize('endpoint', ['workflow', 'chain'])
def test_original_only_assembly_does_not_schedule_or_execute_unrelated_dubbing_repair(repair_flow, endpoint):
    client, uid, calls, assembled = repair_flow
    save('project:' + uid, {
        'assembly.versions': [dict(id='original', name='隔离原声版', dubbing=False,
            burn_subtitles=False, include_comments=False, overrides={})],
        'publish.version_ids': ['original']})
    if endpoint == 'workflow':
        base = f'/api/projects/{uid}/workflow'
        response = client.post(base + '/plan', json=workflow_intent())
        assert response.status_code == 200, response.text
        candidate = response.json()
        assert candidate['steps'] == ['assemble']
        assert not candidate['dubbing']['needed']
        assert not candidate['blockers'], candidate['blockers']
        response = client.post(base + '/start', json=dict(recipe=candidate['recipe'],
            fingerprint=candidate['fingerprint'], confirmed=True, request_id=str(uuid4())))
        assert response.status_code == 200, response.text
        assert finish_workflow(response.json()['id'])['status'] == 'completed'
    else:
        held_chain(uid)
        candidate = chain_recovery.plan(uid)
        assert candidate['steps'] == ['assemble'] and not candidate['dubbing']['needed']
        assert candidate['recovery']['start_step'] == 'assemble'
        response = client.post(f'/api/projects/{uid}/chain/resume', json=dict(
            fingerprint=candidate['fingerprint'], confirmed=True, request_id=str(uuid4())))
        assert response.status_code == 200, response.text
        finish_chain(uid)
    assert calls == [('你好', 1.1), ('世界', 1.1)]
    assert assembled == ['assemble']


def test_disabled_repair_full_workflow_is_blocked_without_touching_existing_audio(repair_flow):
    client, uid, calls, assembled = repair_flow
    save('project:' + uid, {'tts.placement.auto_repair': False})
    root = crud.project_dir(uid)
    original = dubbing_edit.take_path(root, 1, 1).read_bytes()
    response = client.post(f'/api/projects/{uid}/workflow/plan', json=workflow_intent(repair_timing=False))
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert candidate['dubbing']['generate'] == candidate['dubbing']['repair'] == 0
    assert any('配音排布' in reason and '自动修复' in reason for reason in candidate['blockers'])
    response = client.post(f'/api/projects/{uid}/workflow/start', json=dict(recipe=candidate['recipe'],
        fingerprint=candidate['fingerprint'], confirmed=True, request_id=str(uuid4())))
    assert response.status_code == 400, response.text
    assert not assembled and calls == [('你好', 1.1), ('世界', 1.1)]
    assert dubbing_edit.take_path(root, 1, 1).read_bytes() == original


def test_disabled_repair_recovery_does_not_claim_audio_conflicts_are_ready(repair_flow):
    client, uid, calls, assembled = repair_flow
    save('project:' + uid, {'tts.placement.auto_repair': False})
    held_chain(uid)
    candidate = chain_recovery.plan(uid, dubbing={'mode': 'fill', 'repair_timing': False})
    assert candidate['dubbing']['generate'] == candidate['dubbing']['repair'] == 0
    assert 'tts' not in candidate['recovery']['execute_steps']
    assert candidate['blockers'], '恢复预检应明确阻止无法自动修复的配音冲突'
    response = client.post(f'/api/projects/{uid}/chain/resume', json=dict(
        fingerprint=candidate['fingerprint'], confirmed=True, request_id=str(uuid4()),
        dubbing={'mode': 'fill', 'repair_timing': False}))
    assert response.status_code == 400, response.text
    assert calls == [('你好', 1.1), ('世界', 1.1)] and not assembled


def test_project_space_checked_assembly_blocks_disabled_repair_before_any_task(repair_flow):
    client, uid, calls, assembled = repair_flow
    save('project:' + uid, {'tts.placement.auto_repair': False})
    body = dict(steps=['assemble'], include_dependencies=True,
                dubbing=dict(mode='fill', repair_timing=False))
    response = client.post(f'/api/projects/{uid}/run_plan', json=body)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert candidate['dubbing']['generate'] == candidate['dubbing']['repair'] == 0
    assert candidate['blockers'], '项目空间检查也应明确阻止尚未解决的音频冲突'
    response = client.post(f'/api/projects/{uid}/run_chain', json={**body,
        'steps': candidate['steps'], 'plan_fingerprint': candidate['fingerprint']})
    assert response.status_code == 400, response.text
    assert calls == [('你好', 1.1), ('世界', 1.1)] and not assembled
